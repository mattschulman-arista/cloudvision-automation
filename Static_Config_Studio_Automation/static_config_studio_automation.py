#!/usr/bin/env python3
#
# This script manages Arista CloudVision's "Static Configuration" Studio.
# It can:
#   - GET:    Read the current configlet/container/device tree and output it as YAML
#   - SET:    Push a new configlet/container/device tree from a YAML file (or hardcoded INVENTORY)
#   - RECONCILE: Take device-specific "reconciled" configs that CloudVision generated
#                 and reattach them as proper named configlets on the right devices
#   - CLEANUP:   Remove the original RECONCILE_ configlets after they've been copied
#
# It talks to CloudVision via gRPC using the Resource API (cloudvision Python SDK).

import argparse
import asyncio
import json
import logging
import sys
import uuid
from uuid import NAMESPACE_URL, uuid5

import yaml

# --- CloudVision SDK imports ---
# Each module corresponds to a CloudVision resource type accessed via gRPC.
from cloudvision.api import client as cv_client
from cloudvision.api import fmp  # fmp provides helper types like RepeatedString
from cloudvision.api.arista.configlet import v1 as configlet  # Configlets and assignments
from cloudvision.api.arista.inventory import v1 as inventory  # Device inventory (hostname, serial, etc.)
from cloudvision.api.arista.studio import v1 as studio  # Studio inputs (the studio's configuration data)
from cloudvision.api.arista.tag import v2 as tag  # Device tags (label:value pairs like "Role:Spine")
from cloudvision.api.arista.workspace import v1 as workspace  # Workspaces (staging area for changes)
from cloudvision.cvlib.constants import MAINLINE_WS_ID  # Empty string — refers to the "live" production state

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  QUICK START                                                                ║
# ║                                                                             ║
# ║  Prerequisites:  pip install "cloudvision>=1.29.1" pyyaml                   ║
# ║                                                                             ║
# ║  1. See what's currently configured (outputs YAML):                         ║
# ║     python3 static_config_studio_automation.py \                                ║
# ║         --server 192.0.2.10:443 --token-file token.tok --insecure \         ║
# ║         --operation get                                                     ║
# ║                                                                             ║
# ║  2. Push configlets from a YAML inventory file:                             ║
# ║     python3 static_config_studio_automation.py \                                ║
# ║         --server 192.0.2.10:443 --token-file token.tok --insecure \         ║
# ║         --operation set --inventory-file my_inventory.yaml                  ║
# ║                                                                             ║
# ║  3. Copy reconciled configs into proper named configlets:                   ║
# ║     python3 static_config_studio_automation.py \                                ║
# ║         --server 192.0.2.10:443 --token-file token.tok --insecure \         ║
# ║         --operation reconcile --device-filter leaf1                         ║
# ║                                                                             ║
# ║  Add --build-only to preview changes without submitting them.               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT INVENTORY (used by the "set" operation when --inventory-file is not given)
#
# This dictionary defines which configlets get assigned to which devices,
# organized into a container hierarchy. Think of it like a folder structure:
#
#   "containers" = folders that group devices by location/role/purpose.
#       - "name" uses "/" for nesting: "US/DC1" means DC1 is inside US.
#       - "configlets" on a container apply to ALL devices matched by
#         that container's tag query (e.g., location:DC1).
#
#   "devices" = individual switches/routers identified by serial number.
#       - "device_id" is the device's serial number (or hostname in some setups).
#       - "container" (optional) places the device inside a container.
#       - "configlets" are the config snippets assigned to this specific device.
#
# Each configlet entry can be:
#   {"name": "x", "configlet_file": "path"}  → create a new configlet from a local file
#   {"name": "x"}                            → reference an existing configlet already on CloudVision
#
# WARNING: The "set" operation recreates the container hierarchy from scratch.
# Any containers you manually created INSIDE the defined hierarchy will be
# deleted on the next run. Containers OUTSIDE the hierarchy are untouched.
# ─────────────────────────────────────────────────────────────────────────────

INVENTORY = {
    "containers": [
        {
            "name": "US/DC1",
            "configlets": [
                {"name": "ntp_dc1", "configlet_file": "configlets/ntp_dc1.cfg"},
            ],
        },
    ],
    "devices": [
        {
            "device_id": "JPE21231033",
            "container": "US/DC1",
            "configlets": [
                {"name": "leaf1", "configlet_file": "configlets/leaf1.cfg"},
                # {"name": "leaf1_exception", }
            ],
        },
        {
            "device_id": "JPE21231032",
            "container": "US/DC2",
            "configlets": [
                {"name": "leaf2", "configlet_file": "configlets/leaf2.cfg"},
                # {"name": "leaf2_exception"},
            ],
        },
    ],
}


logger = logging.getLogger(__name__)

# The built-in CloudVision studio that manages static configlet assignments.
STATIC_CONFIGLET_STUDIO_ID = "studio-static-configlet"
RPC_TIMEOUT = 30  # Seconds to wait for a single API call
BUILD_TIMEOUT = 300  # Seconds to wait for a workspace build (compiling configs takes longer)

# A fixed namespace for generating deterministic UUIDs. Using uuid5 with this
# namespace means the same configlet name always produces the same ID, making
# the script idempotent — running it twice won't create duplicates.
ID_NAMESPACE = uuid5(NAMESPACE_URL, "assign-static-configlet")


def create_client(args):
    """Create a gRPC connection to the CloudVision server using a service account token."""
    token = args.token_file.read().strip()
    host_parts = args.server.split(":")
    host = host_parts[0]
    port = int(host_parts[1]) if len(host_parts) > 1 else 443
    return cv_client.AsyncCVClient.from_token(
        token=token,
        host=host,
        port=port,
        cacert=args.cert_file,
        insecure=args.insecure,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET OPERATION — Read the current state from CloudVision
#
# The "get" operation reads the live (mainline) configlet tree from CloudVision
# and outputs it in a YAML format that matches the INVENTORY structure.
# This lets you capture the current state, filter by device, and optionally
# save to a file that can later be used with "set" to reproduce the config.
# ─────────────────────────────────────────────────────────────────────────────


async def get_configlet_id_to_info(channel):
    """Fetch every configlet from CloudVision and build a lookup table.

    Returns a dict mapping configlet ID -> {name, body} so we can translate
    the opaque UUIDs used internally into human-readable names and content.
    We request include_body=True so the actual config text is returned.
    """
    stub = configlet.ConfigletServiceStub(channel)
    req = configlet.ConfigletStreamRequest(
        partial_eq_filter=[configlet.Configlet(key=configlet.ConfigletKey(workspace_id=MAINLINE_WS_ID))],
        filter=configlet.Filter(include_body=True),
    )
    id_map = {}
    async for resp in stub.get_all(req, timeout=RPC_TIMEOUT):
        c = resp.value
        id_map[c.key.configlet_id] = {"name": c.display_name, "body": c.body or ""}
    return id_map


async def get_inventory(channel, include_reconcile=False):
    """Walk the assignment tree from studio roots and return an INVENTORY-format dict.

    CloudVision's Static Configuration Studio organizes configlets in a tree:
      - The studio has a list of "root" assignment IDs (configletAssignmentRoots).
      - Each assignment has a "query" that matches devices (e.g., "device:SERIAL",
        "Campus:HQ", "Role:Spine") and optionally has child assignments forming a tree.
      - Leaf assignments with "device:" queries are individual device entries.
      - Non-leaf assignments (or ones with tag queries) are containers/groups.

    This function walks that tree and produces a dict with "containers", "devices",
    and optionally "reconcile" keys — matching the INVENTORY format used by "set".
    """
    # Step 1: Fetch all the raw data we need from CloudVision
    id_to_info = await get_configlet_id_to_info(channel)  # configlet ID -> {name, body}
    assignments = await get_all_assignments(channel)  # assignment ID -> {name, query, configlet_ids, children}
    inputs = await get_studio_inputs(channel)  # Studio config including the list of root assignment IDs
    roots = inputs.get("configletAssignmentRoots", []) if inputs else []

    # When --debug is enabled, dump raw data to files for troubleshooting
    if logger.isEnabledFor(logging.DEBUG):
        with open("debug_studio_inputs.json", "w") as _dbg:
            json.dump(inputs, _dbg, indent=2, default=str)
        logger.debug("Studio inputs written to debug_studio_inputs.json")
        with open("debug_assignments.json", "w") as _dbg:
            json.dump({"roots": roots, "assignments": assignments}, _dbg, indent=2, default=str)
        logger.debug("Assignments written to debug_assignments.json")
        with open("debug_configlets.json", "w") as _dbg:
            json.dump(id_to_info, _dbg, indent=2, default=str)
        logger.debug("Configlets written to debug_configlets.json")

    containers = []
    devices = []

    def _configlet_entries(configlet_ids):
        """Convert a list of configlet IDs into human-readable entries with name and body."""
        entries = []
        for cid in configlet_ids:
            info = id_to_info.get(cid)
            if info:
                entries.append({"name": info["name"], "body": info["body"] or "(empty)"})
        return entries

    def _walk(assignment_id, parent_path=""):
        """Recursively walk the assignment tree, classifying each node as a
        device (leaf with device: query) or a container (everything else).

        parent_path tracks the "/" separated path through the tree so we can
        show where each device or container sits in the hierarchy.
        """
        ainfo = assignments.get(assignment_id)
        if not ainfo:
            return

        query = ainfo.get("query", "")
        children = ainfo.get("child_assignment_ids", [])
        cfgs = _configlet_entries(ainfo["configlet_ids"])
        display_name = ainfo.get("name", "")

        # A "device leaf" is an assignment that targets specific device(s)
        # and has no children — it's the bottom of the tree.
        is_device_leaf = query.startswith("device:") and not children

        if is_device_leaf:
            # Extract the device ID(s) from the query (e.g., "device:SERIAL" -> "SERIAL")
            device_id = query[len("device:") :]
            entry = {"device_id": device_id}
            if display_name:
                entry["name"] = display_name
            if parent_path:
                entry["container"] = parent_path
            if cfgs:
                entry["configlets"] = cfgs
            devices.append(entry)
        else:
            # This is a container/group node — it organizes devices underneath it.
            # Build its path from its display name (or query if unnamed).
            path = display_name or query
            if parent_path:
                path = f"{parent_path}/{path}"

            container_entry = {"name": path}
            if parent_path:
                container_entry["parent"] = parent_path
            container_entry["query"] = query
            container_entry["configlets"] = cfgs if cfgs else []
            containers.append(container_entry)

            # Recurse into children, passing this container's path as the parent
            for child_id in children:
                _walk(child_id, path)

    # Step 2: Walk the tree starting from each root assignment.
    # Skip RECONCILE_TREE_KEY — those are handled separately in the "reconcile" section
    # to avoid showing the same data twice.
    for root_id in roots:
        if root_id == "RECONCILE_TREE_KEY":
            continue
        _walk(root_id)

    result = {}
    if containers:
        result["containers"] = containers
    if devices:
        result["devices"] = devices

    # Step 3: Optionally include reconcile configlets as a separate section.
    # These are configlets CloudVision created during a "reconcile" operation on a device —
    # they capture the difference between the designed config and what's actually running.
    # Their IDs start with "RECONCILE_" followed by the device's hash.
    if include_reconcile:
        reconciled = await get_reconciled_configlets(channel)
        if reconciled:
            reconcile_list = []
            for cid, display_name, device_hash, body in reconciled:
                # Display name format is "Reconcile <hostname> <timestamp>"
                parts = display_name.split(" ")
                hostname = parts[1] if len(parts) >= 2 else device_hash
                entry = {
                    "device_id": device_hash,
                    "hostname": hostname,
                    "configlet_id": cid,
                    "display_name": display_name,
                    "body": body or "(empty)",
                }
                reconcile_list.append(entry)
            result["reconcile"] = reconcile_list

    return result


async def resolve_hostname_filter(channel, hostname_filter):
    """Translate a hostname filter into device serial numbers.

    Users think in hostnames (e.g., "leaf1", "spine2"), but CloudVision internally
    identifies devices by serial number / hash. This function queries the device
    inventory, finds all devices whose hostname contains the filter string
    (substring match), and returns their serial numbers so we can filter the
    assignment tree by device ID.
    """
    stub = inventory.DeviceServiceStub(channel)
    req = inventory.DeviceStreamRequest()
    matching_ids = set()
    async for resp in stub.get_all(req, timeout=RPC_TIMEOUT):
        device = resp.value
        if device.hostname and hostname_filter in device.hostname:
            matching_ids.add(device.key.device_id)
    return matching_ids


async def get_device_tags(channel, device_id):
    """Fetch all tag label:value pairs assigned to a device from mainline.

    In CloudVision, every device has tags — key-value pairs like "Role:Spine",
    "Campus:HQ", "software_version:4.35.4M". Some are system-generated (hostname,
    model, etc.), others are user-created. Containers use tag queries to match
    devices, so we need a device's tags to determine which containers apply to it.
    """
    stub = tag.TagAssignmentServiceStub(channel)
    req = tag.TagAssignmentStreamRequest(
        partial_eq_filter=[
            tag.TagAssignment(
                key=tag.TagAssignmentKey(
                    workspace_id=MAINLINE_WS_ID,
                    element_type=tag.ElementType.DEVICE,
                    device_id=device_id,
                )
            )
        ]
    )
    tags = set()
    async for resp in stub.get_all(req, timeout=RPC_TIMEOUT):
        tags.add((resp.value.key.label, resp.value.key.value))
    return tags


def _query_matches_tags(query, device_tags, matching_device_ids=None):
    """Check if a container's query would match a device based on its tags.

    Container queries use the format "label:value" with optional "AND" for
    compound conditions. Examples from real CloudVision setups:
      - "Campus:HQ"                          → device must have tag Campus=HQ
      - "Campus-Pod:Bldg1 AND Role:Spine"    → device must have BOTH tags
      - "device:*"                            → matches all devices
      - "device:SERIAL1,SERIAL2"             → matches specific device(s) by ID
      - "Role:*"                             → any device with a Role tag

    This is used by --include-tag-matches to show which containers would
    apply to a filtered device.
    """
    if query.startswith("device:"):
        # device: queries match by device ID, not by tags
        device_value = query[len("device:") :]
        if device_value == "*":
            return True  # Wildcard — matches every device
        if matching_device_ids:
            # Check if any of the comma-separated device IDs are in our filter set
            return any(did in matching_device_ids for did in device_value.split(","))
        return False
    # Tag-based query: split on " AND " and check each condition
    conditions = [c.strip() for c in query.split(" AND ")]
    for condition in conditions:
        if ":" not in condition:
            return False
        label, value = condition.split(":", 1)
        if value == "*":
            # Wildcard value — just check that the device has ANY tag with this label
            if not any(lab == label for lab, _ in device_tags):
                return False
        elif (label, value) not in device_tags:
            return False  # Device doesn't have this specific tag
    return True  # All conditions matched


# ─────────────────────────────────────────────────────────────────────────────
# STUDIO INPUTS — Reading the studio's configuration
#
# CloudVision studios store their configuration as "inputs" — JSON blobs
# organized by path. The Static Configuration Studio stores one key piece
# of data: "configletAssignmentRoots" — a list of assignment IDs that form
# the top level of the configlet tree.
#
# The API returns inputs split across multiple responses at different paths.
# We need to deep-merge them into a single nested dict (like assembling a
# jigsaw puzzle from pieces). The _merge_inputs function handles this.
# ─────────────────────────────────────────────────────────────────────────────


def _merge_inputs(root, path, inputs):
    """Deep-merge studio inputs at a given path into root.

    CloudVision returns studio inputs as multiple (path, data) pairs.
    For example, the root path [] might return {"configletAssignmentRoots": [...]},
    while a sub-path ["configletAssignmentRoots", "0"] returns details about
    the first root entry. This function reassembles them into one nested dict.

    Mirrors the mergeInputs() logic in studio_update.py.
    """
    prev_elem = None
    prev = root
    curr = root

    for curr_elem in path:
        if curr_elem.isnumeric():
            if not isinstance(curr, list):
                if prev_elem is None:
                    root = []
                    curr = root
                elif prev_elem.isnumeric():
                    prev[int(prev_elem)] = []
                    curr = prev[int(prev_elem)]
                else:
                    prev[prev_elem] = []
                    curr = prev[prev_elem]
            idx = int(curr_elem)
            if idx >= len(curr):
                while len(curr) < idx + 1:
                    curr.append(None)
            prev_elem = curr_elem
            prev = curr
            curr = curr[idx]
        else:
            if not isinstance(curr, dict):
                if prev_elem is None:
                    root = {}
                    curr = root
                elif prev_elem.isnumeric():
                    prev[int(prev_elem)] = {}
                    curr = prev[int(prev_elem)]
                else:
                    prev[prev_elem] = {}
                    curr = prev[prev_elem]
            if curr_elem not in curr:
                curr[curr_elem] = None
            prev_elem = curr_elem
            prev = curr
            curr = curr[curr_elem]

    if isinstance(curr, dict):
        curr.update(inputs)
    else:
        if prev_elem is None:
            root = inputs
        elif prev_elem.isnumeric():
            prev[int(prev_elem)] = inputs
        else:
            prev[prev_elem] = inputs
    return root


async def get_studio_inputs(channel):
    """Fetch and merge all studio inputs for the Static Configuration Studio.

    Returns a dict like {"configletAssignmentRoots": ["id1", "id2", ...]}.
    """
    key = studio.InputsKey(
        studio_id=STATIC_CONFIGLET_STUDIO_ID,
        workspace_id=MAINLINE_WS_ID,
    )
    req = studio.InputsStreamRequest()
    req.partial_eq_filter.append(studio.Inputs(key=key))
    stub = studio.InputsServiceStub(channel)
    merged = None
    async for resp in stub.get_all(req, timeout=RPC_TIMEOUT):
        path = resp.value.key.path.values
        split = json.loads(resp.value.inputs)
        merged = _merge_inputs(merged, path, split)
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# WORKSPACE LIFECYCLE — How changes get applied in CloudVision
#
# CloudVision never applies changes directly to the live network. Instead:
#   1. CREATE a workspace — a private staging area for your changes
#   2. Make changes inside the workspace (create configlets, assignments, etc.)
#   3. BUILD the workspace — CloudVision compiles and validates all changes
#   4. SUBMIT the workspace — pushes changes to the live ("mainline") state
#   5. Change Controls are created for each affected device, which can then
#      be approved and executed to push config to the actual switches
#
# This is similar to a git branch: you work in isolation, then merge.
# ─────────────────────────────────────────────────────────────────────────────


async def create_workspace(channel, name):
    """Create a new workspace (staging area) for making changes."""
    ws_id = str(uuid.uuid4())
    req = workspace.WorkspaceConfigSetRequest(
        value=workspace.WorkspaceConfig(
            key=workspace.WorkspaceKey(workspace_id=ws_id),
            display_name=name,
        )
    )
    stub = workspace.WorkspaceConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)
    logger.info("Workspace created: %s", ws_id)
    return ws_id


async def build_workspace(channel, ws_id):
    """Build (compile and validate) all changes in a workspace.

    This triggers CloudVision to check that all configlets are valid,
    compile any templates, and verify the resulting config won't cause errors.
    We subscribe to workspace events and wait for the build result.
    Returns True if the build succeeded, False otherwise.
    """
    build_id = str(uuid.uuid4())
    req = workspace.WorkspaceConfigSetRequest(
        value=workspace.WorkspaceConfig(
            key=workspace.WorkspaceKey(workspace_id=ws_id),
            request=workspace.Request.START_BUILD,
            request_params=workspace.RequestParams(request_id=build_id),
        )
    )
    stub = workspace.WorkspaceConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)
    logger.info("Build request %s sent", build_id)

    req = workspace.WorkspaceStreamRequest(
        partial_eq_filter=[workspace.Workspace(key=workspace.WorkspaceKey(workspace_id=ws_id))]
    )
    stub = workspace.WorkspaceServiceStub(channel)
    logger.info("Waiting for build to complete...")
    async for res in stub.subscribe(req, timeout=BUILD_TIMEOUT):
        if build_id in res.value.responses.values:
            build_res = res.value.responses.values[build_id]
            break

    if build_res.status == workspace.ResponseStatus.SUCCESS:
        logger.info("Build succeeded")
        return True

    logger.error("Build failed")
    fail_msg = await get_build_failure_message(channel, ws_id, build_id)
    if fail_msg:
        logger.error("Build details:\n%s", fail_msg)
    return False


async def get_build_failure_message(channel, ws_id, build_id):
    """When a build fails, fetch detailed error messages explaining what went wrong.

    Errors can come from three stages:
      - INPUT_VALIDATION: the studio inputs don't match the expected schema
      - CONFIGLET_BUILD: a configlet template failed to compile
      - CONFIG_VALIDATION: the resulting device config has errors (e.g., invalid syntax)
    """
    fail_msg = ""
    req = workspace.WorkspaceBuildDetailsStreamRequest(
        partial_eq_filter=[
            workspace.WorkspaceBuildDetails(
                key=workspace.WorkspaceBuildDetailsKey(
                    workspace_id=ws_id,
                    build_id=build_id,
                )
            )
        ]
    )
    stub = workspace.WorkspaceBuildDetailsServiceStub(channel)
    async for resp in stub.get_all(req, timeout=RPC_TIMEOUT):
        result = resp.value
        if result.state == workspace.BuildState.FAIL:
            dev_id = result.key.device_id
            fail_msg += f"  Device {dev_id}:\n"
            if result.stage == workspace.BuildStage.INPUT_VALIDATION:
                fail_msg += "    Input validation errors:\n"
                for sid, ivr in result.input_validation_results.values.items():
                    for err in ivr.input_schema_errors.values:
                        fail_msg += f"      Schema: {err.message}\n"
                    for err in ivr.input_value_errors.values:
                        fail_msg += f"      Value: {err.message}\n"
                    for err in ivr.other_errors.values:
                        fail_msg += f"      Other: {err}\n"
            if result.stage == workspace.BuildStage.CONFIGLET_BUILD:
                fail_msg += "    Configlet compilation errors:\n"
                for sid, cbr in result.configlet_build_results.values.items():
                    for err in cbr.template_errors.values:
                        fail_msg += f"      Line {err.line_num}: {err.exception}\n"
            if result.stage == workspace.BuildStage.CONFIG_VALIDATION:
                fail_msg += "    Config validation errors:\n"
                for err in result.config_validation_result.errors.values:
                    fail_msg += f"      {err.configlet_name} line {err.line_num}: {err.error_msg}\n"
    return fail_msg


async def submit_workspace(channel, ws_id):
    """Submit a workspace to apply its changes to the live state.

    After a successful build, submitting merges the workspace into mainline.
    CloudVision creates Change Control(s) for each affected device, which
    must be approved and executed to actually push config to the switches.
    Returns (change_control_ids, success_bool).
    """
    submit_id = str(uuid.uuid4())
    req = workspace.WorkspaceConfigSetRequest(
        value=workspace.WorkspaceConfig(
            key=workspace.WorkspaceKey(workspace_id=ws_id),
            request=workspace.Request.SUBMIT,
            request_params=workspace.RequestParams(request_id=submit_id),
        )
    )
    stub = workspace.WorkspaceConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)
    logger.info("Submission request %s sent", submit_id)

    req = workspace.WorkspaceStreamRequest(
        partial_eq_filter=[workspace.Workspace(key=workspace.WorkspaceKey(workspace_id=ws_id))]
    )
    stub = workspace.WorkspaceServiceStub(channel)
    logger.info("Waiting for submission to complete...")
    async for res in stub.subscribe(req, timeout=RPC_TIMEOUT):
        if submit_id in res.value.responses.values:
            submit_res = res.value.responses.values[submit_id]
            if submit_res.status == workspace.ResponseStatus.FAIL:
                logger.error("Submission failed: %s", submit_res.message)
                return None, False
            if submit_res.status == workspace.ResponseStatus.SUCCESS:
                logger.info("Submission succeeded")
        if res.value.state == workspace.WorkspaceState.SUBMITTED:
            return res.value.cc_ids.values, True
    logger.error("Submission failed")
    return None, False


async def abandon_workspace(channel, ws_id):
    """Abandon a workspace so it doesn't remain open on CloudVision."""
    req = workspace.WorkspaceConfigSetRequest(
        value=workspace.WorkspaceConfig(
            key=workspace.WorkspaceKey(workspace_id=ws_id),
            request=workspace.Request.ABANDON,
            request_params=workspace.RequestParams(request_id=str(uuid.uuid4())),
        )
    )
    stub = workspace.WorkspaceConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)
    logger.info("Workspace abandoned: %s", ws_id)


# ─────────────────────────────────────────────────────────────────────────────
# SET OPERATION — Push configlets to CloudVision
#
# The "set" operation takes an INVENTORY (from a YAML file or the hardcoded
# default) and creates/updates configlets, assignments, and containers in
# CloudVision. It:
#   1. Creates a workspace for staging changes
#   2. Creates or resolves each configlet (from file or by name lookup)
#   3. Creates assignments linking configlets to devices
#   4. Builds a container tree with tag-based queries for grouping
#   5. Assigns location tags to devices so container queries match
#   6. Builds and submits the workspace
# ─────────────────────────────────────────────────────────────────────────────


async def get_configlet_name_to_id(channel):
    """Build a display_name -> configlet_id map from mainline.

    Used by the "set" operation to look up existing configlets by name
    when an INVENTORY entry doesn't specify a configlet_file.
    """
    stub = configlet.ConfigletServiceStub(channel)
    req = configlet.ConfigletStreamRequest(
        partial_eq_filter=[configlet.Configlet(key=configlet.ConfigletKey(workspace_id=MAINLINE_WS_ID))]
    )
    name_map = {}
    async for resp in stub.get_all(req, timeout=RPC_TIMEOUT):
        c = resp.value
        name_map[c.display_name] = c.key.configlet_id
    return name_map


def validate_inventory(inventory, existing_configlets):
    """Validate that all configlet entries in the inventory can be resolved.

    Checks every configlet entry in containers and devices to ensure it has
    a body, a configlet_file that exists, or a name that exists on CloudVision.
    Returns a list of error messages (empty if everything is valid).
    """
    import os

    errors = []
    for section in ("containers", "devices"):
        for item in inventory.get(section, []):
            item_label = item.get("name", item.get("device_id", "unknown"))
            for entry in item.get("configlets", []):
                name = entry.get("name", "(unnamed)")
                if "body" in entry:
                    continue
                if "configlet_file" in entry:
                    if not os.path.isfile(entry["configlet_file"]):
                        errors.append(f'Configlet "{name}": file "{entry["configlet_file"]}" not found')
                    continue
                if name not in existing_configlets:
                    errors.append(
                        f'Configlet "{name}" (in {section} "{item_label}"): '
                        f"no body or configlet_file, and not found on CloudVision"
                    )
    return errors


async def resolve_configlet(channel, ws_id, entry, existing_configlets):
    """Return the configlet ID for an INVENTORY entry.

    Three modes, checked in order:
    - ``body`` given → create a new configlet with that body text directly.
    - ``configlet_file`` given → create a new configlet from the file contents.
    - name-only → look up an existing configlet by display name on CloudVision.
    """
    if "body" in entry:
        # Body is provided inline in the inventory — use it directly
        return await create_configlet(channel, ws_id, entry["name"], entry["body"])

    if "configlet_file" in entry:
        # Body is in an external file — read it
        with open(entry["configlet_file"]) as f:
            configlet_body = f.read()
        return await create_configlet(channel, ws_id, entry["name"], configlet_body)

    # No body or file — try to find the configlet by name on CloudVision
    cid = existing_configlets.get(entry["name"])
    if cid is None:
        logger.error(
            'Configlet "%s" has no body or configlet_file, and was not found on CloudVision',
            entry["name"],
        )
        sys.exit(1)
    logger.info('Resolved existing configlet "%s" -> %s', entry["name"], cid)
    return cid


async def create_configlet(channel, ws_id, configlet_name, configlet_body):
    """Create a configlet in a workspace.

    The configlet ID is generated deterministically from the name using uuid5,
    so creating the same configlet twice produces the same ID (idempotent).
    """
    configlet_id = str(uuid5(ID_NAMESPACE, f"configlet:{configlet_name}"))
    req = configlet.ConfigletConfigSetRequest(
        value=configlet.ConfigletConfig(
            key=configlet.ConfigletKey(
                workspace_id=ws_id,
                configlet_id=configlet_id,
            ),
            display_name=configlet_name,
            body=configlet_body,
        )
    )
    stub = configlet.ConfigletConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)
    logger.info("Configlet created: %s (ID: %s)", configlet_name, configlet_id)
    return configlet_id


async def create_assignment(channel, ws_id, device_id, device_query, configlet_ids):
    """Create a configlet assignment — the link between configlets and a device.

    An assignment says "apply these configlets to devices matching this query."
    The query is usually "device:SERIAL" for a specific device, but can also
    be a tag query like "Role:Spine" to match groups of devices.
    """
    assignment_id = str(uuid5(ID_NAMESPACE, f"assignment:{device_id}"))
    req = configlet.ConfigletAssignmentConfigSetRequest(
        value=configlet.ConfigletAssignmentConfig(
            key=configlet.ConfigletAssignmentKey(
                workspace_id=ws_id,
                configlet_assignment_id=assignment_id,
            ),
            query=device_query,
            configlet_ids=fmp.RepeatedString(values=configlet_ids),
            match_policy=configlet.MatchPolicy.MATCH_FIRST,
        )
    )
    stub = configlet.ConfigletAssignmentConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)
    logger.info("Assignment created: %s -> %s (ID: %s)", device_query, configlet_ids, assignment_id)
    return assignment_id


async def create_container(channel, ws_id, container_id, display_name, child_assignment_ids, configlet_ids=None):
    """Create a container — a grouping node in the assignment tree.

    Containers are actually ConfigletAssignments with child_assignment_ids.
    They use a tag query (e.g., "location:DC1") to match devices, and their
    children can be other containers or device-level assignments.
    """
    req = configlet.ConfigletAssignmentConfigSetRequest(
        value=configlet.ConfigletAssignmentConfig(
            key=configlet.ConfigletAssignmentKey(
                workspace_id=ws_id,
                configlet_assignment_id=container_id,
            ),
            display_name=display_name,
            description="Container created by assign_static_configlet.py",
            query=f"location:{display_name}",
            configlet_ids=fmp.RepeatedString(values=configlet_ids or []),
            child_assignment_ids=fmp.RepeatedString(values=child_assignment_ids),
            match_policy=configlet.MatchPolicy.MATCH_FIRST,
        )
    )
    stub = configlet.ConfigletAssignmentConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)
    logger.info(
        "Container created: %s (ID: %s) with %d children", display_name, container_id, len(child_assignment_ids)
    )


async def update_studio_roots(channel, ws_id, new_assignment_ids):
    """Add new assignment IDs to the studio's root list (without removing existing ones).

    The "configletAssignmentRoots" list in the studio inputs defines which
    assignments sit at the top level of the tree. This function appends new
    IDs to the existing list, avoiding duplicates.
    """
    current_inputs = await get_studio_inputs(channel)
    existing_roots = current_inputs.get("configletAssignmentRoots", []) if current_inputs else []
    existing_set = set(existing_roots)
    updated_roots = existing_roots + [aid for aid in new_assignment_ids if aid not in existing_set]

    inputs_json = json.dumps({"configletAssignmentRoots": updated_roots})
    req = studio.InputsConfigSetRequest(
        value=studio.InputsConfig(
            key=studio.InputsKey(
                workspace_id=ws_id,
                studio_id=STATIC_CONFIGLET_STUDIO_ID,
                path=fmp.RepeatedString(values=[]),
            ),
            inputs=inputs_json,
        )
    )
    stub = studio.InputsConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)
    logger.info("Studio inputs updated with assignment roots: %s", updated_roots)


# ─────────────────────────────────────────────────────────────────────────────
# TAGS — Labeling devices so container queries can match them
#
# CloudVision uses tags (label:value pairs) to organize devices.
# When the "set" operation creates containers with "location:DC1" queries,
# it also needs to tag each device with "location=DC1" so the query matches.
# Tags are created in the workspace and applied to devices.
# ─────────────────────────────────────────────────────────────────────────────


async def create_tag_if_needed(channel, ws_id, label, value, created_tags):
    """Create a tag (label:value) unless already created in this run."""
    if (label, value) in created_tags:
        return
    req = tag.TagConfigSetRequest(
        value=tag.TagConfig(
            key=tag.TagKey(
                workspace_id=ws_id,
                element_type=tag.ElementType.DEVICE,
                label=label,
                value=value,
            ),
        )
    )
    stub = tag.TagConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)
    created_tags.add((label, value))
    logger.info("Tag created: %s:%s", label, value)


async def assign_tag_to_device(channel, ws_id, device_id, label, value):
    req = tag.TagAssignmentConfigSetRequest(
        value=tag.TagAssignmentConfig(
            key=tag.TagAssignmentKey(
                workspace_id=ws_id,
                element_type=tag.ElementType.DEVICE,
                label=label,
                value=value,
                device_id=device_id,
            ),
        )
    )
    stub = tag.TagAssignmentConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)
    logger.info("Tag %s:%s assigned to device %s", label, value, device_id)


# ─────────────────────────────────────────────────────────────────────────────
# RECONCILE OPERATION — Copy reconciled configs into proper configlets
#
# When you "reconcile" a device in CloudVision, it captures the difference
# between the designed config and what's actually running on the switch.
# CloudVision stores this as a configlet with ID "RECONCILE_<device_hash>".
#
# The problem: these RECONCILE_ configlets are temporary and managed by
# CloudVision's built-in reconcile tree (RECONCILE_TREE_KEY). They're not
# part of the normal configlet assignment tree.
#
# The "reconcile" operation in this script:
#   1. Finds all RECONCILE_ configlets
#   2. Creates proper named configlets (e.g., "leaf1-reconciled") with the same body
#   3. Attaches them to the device's existing assignment in the normal tree
#   4. Cleans up orphan assignments and old RECONCILE_ references
#
# The "cleanup-reconciled" operation then deletes the original RECONCILE_ configlets
# and removes the RECONCILE_TREE_KEY from the studio roots.
# ─────────────────────────────────────────────────────────────────────────────


async def get_reconciled_configlets(channel):
    """Find all RECONCILE_ configlets in mainline.

    Returns a list of (configlet_id, display_name, device_hash, body) tuples.
    The device_hash is extracted from the configlet ID — it's the part after
    "RECONCILE_" and identifies which device this reconciled config belongs to.
    """
    stub = configlet.ConfigletServiceStub(channel)
    req = configlet.ConfigletStreamRequest(
        partial_eq_filter=[configlet.Configlet(key=configlet.ConfigletKey(workspace_id=MAINLINE_WS_ID))],
        filter=configlet.Filter(include_body=True),
    )
    results = []
    prefix = "RECONCILE_"
    async for resp in stub.get_all(req, timeout=RPC_TIMEOUT):
        c = resp.value
        if c.key.configlet_id.startswith(prefix):
            device_hash = c.key.configlet_id[len(prefix) :]
            results.append((c.key.configlet_id, c.display_name, device_hash, c.body or ""))
    return results


async def get_all_assignments(channel):
    """Return a dict of assignment_id -> assignment details from mainline."""
    stub = configlet.ConfigletAssignmentServiceStub(channel)
    req = configlet.ConfigletAssignmentStreamRequest(
        partial_eq_filter=[
            configlet.ConfigletAssignment(key=configlet.ConfigletAssignmentKey(workspace_id=MAINLINE_WS_ID))
        ]
    )
    assignments = {}
    async for resp in stub.get_all(req, timeout=RPC_TIMEOUT):
        a = resp.value
        assignments[a.key.configlet_assignment_id] = {
            "name": a.display_name,
            "query": a.query,
            "configlet_ids": list(a.configlet_ids.values),
            "child_assignment_ids": list(a.child_assignment_ids.values),
        }
    return assignments


async def update_assignment_configlets(channel, ws_id, assignment_id, configlet_ids):
    """Replace an existing assignment's configlet list within a workspace.

    Used during reconcile to add the new "-reconciled" configlet to a device's
    assignment, and to remove old RECONCILE_ references.
    """
    req = configlet.ConfigletAssignmentConfigSetRequest(
        value=configlet.ConfigletAssignmentConfig(
            key=configlet.ConfigletAssignmentKey(
                workspace_id=ws_id,
                configlet_assignment_id=assignment_id,
            ),
            configlet_ids=fmp.RepeatedString(values=configlet_ids),
        )
    )
    stub = configlet.ConfigletAssignmentConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)


async def delete_assignment(channel, ws_id, assignment_id):
    """Delete a configlet assignment within a workspace."""
    req = configlet.ConfigletAssignmentConfigDeleteRequest(
        key=configlet.ConfigletAssignmentKey(
            workspace_id=ws_id,
            configlet_assignment_id=assignment_id,
        )
    )
    stub = configlet.ConfigletAssignmentConfigServiceStub(channel)
    await stub.delete(req, timeout=RPC_TIMEOUT)


async def set_studio_roots(channel, ws_id, root_ids):
    """Overwrite the configletAssignmentRoots list in the studio inputs.

    Unlike update_studio_roots (which appends), this replaces the entire list.
    Used during reconcile cleanup to remove orphan entries.
    """
    inputs_json = json.dumps({"configletAssignmentRoots": root_ids})
    req = studio.InputsConfigSetRequest(
        value=studio.InputsConfig(
            key=studio.InputsKey(
                workspace_id=ws_id,
                studio_id=STATIC_CONFIGLET_STUDIO_ID,
                path=fmp.RepeatedString(values=[]),
            ),
            inputs=inputs_json,
        )
    )
    stub = studio.InputsConfigServiceStub(channel)
    await stub.set(req, timeout=RPC_TIMEOUT)


async def resolve_assignment_root(assignments, root_arg):
    """Resolve an assignment root by ID or display name."""
    if root_arg in assignments:
        return root_arg
    for aid, ainfo in assignments.items():
        if ainfo["name"] == root_arg:
            return aid
    return None


def _device_query_matches(query, device_hash, device_name=None):
    """Check if a "device:" query matches a device by its hash (serial number) or hostname.

    Device queries can reference devices by serial number OR hostname, and
    can list multiple devices comma-separated (e.g., "device:leaf1,leaf2").
    The wildcard "device:*" is intentionally NOT matched here — it would
    match everything and isn't useful for finding a specific device's assignment.
    """
    if not query.startswith("device:"):
        return False
    value = query[len("device:") :]
    if value == "*":
        return False  # Skip wildcard — not a specific device match
    ids = value.split(",")
    if device_hash in ids:
        return True
    if device_name and device_name in ids:
        return True
    return False


def find_device_assignment(assignments, device_hash, device_name=None):
    """Find where a device is currently assigned in the configlet tree.

    Returns (assignment_id, parent_container_id) or (None, None).

    This is used by the reconcile operation to figure out where to attach
    the reconciled configlet. We need to find the device's existing assignment
    so we can add the new configlet there, rather than creating a duplicate.

    Why the complexity:
      - Devices can be referenced by serial number (hash) OR hostname — we check both.
      - RECONCILE_TREE_KEY and its children must be skipped — those are the temporary
        reconcile assignments, not the device's "real" location in the tree.
      - device:* containers must be skipped — they match all devices and aren't
        meaningful as a specific attachment point.
      - A device might appear in multiple places; we prefer the assignment that
        already has real (non-RECONCILE_) configlets attached.
    """
    # Build a set of assignment IDs to ignore — the reconcile tree and its children
    reconcile_tree = assignments.get("RECONCILE_TREE_KEY", {})
    skip_ids = {"RECONCILE_TREE_KEY"}
    skip_ids.update(reconcile_tree.get("child_assignment_ids", []))

    # Search all assignments for ones matching this device (by hash or hostname)
    candidates = []
    for aid, ainfo in assignments.items():
        if aid in skip_ids:
            continue

        # Check if this assignment itself targets the device
        if _device_query_matches(ainfo.get("query", ""), device_hash, device_name):
            # Found a match — now find its parent container (if any)
            parent_id = None
            for parent_aid, parent_info in assignments.items():
                if parent_aid in skip_ids:
                    continue
                if aid in parent_info.get("child_assignment_ids", []):
                    if parent_info.get("query") != "device:*":
                        parent_id = parent_aid
                        break
            candidates.append((aid, ainfo, parent_id))
            continue

        # Also check if this assignment's children target the device
        for child_id in ainfo.get("child_assignment_ids", []):
            if child_id in skip_ids:
                continue
            child = assignments.get(child_id)
            if not child:
                continue
            if _device_query_matches(child.get("query", ""), device_hash, device_name):
                # Skip if parent is device:* (wildcard catch-all, not a real container)
                if ainfo.get("query") == "device:*":
                    continue
                candidates.append((child_id, child, aid))

    if not candidates:
        return None, None

    # If multiple matches, prefer the one that already has real configlets attached.
    # This avoids picking an empty placeholder assignment over one with actual config.
    for cand_id, cand_info, parent_id in candidates:
        real_cfgs = [c for c in cand_info.get("configlet_ids", []) if not c.startswith("RECONCILE_")]
        if real_cfgs:
            return cand_id, parent_id
    # Fall back to the first candidate if none have real configlets
    return candidates[0][0], candidates[0][2]


async def reconcile_configlets(channel, args):
    """Main reconcile operation — copy RECONCILE_ configlets into proper named configlets.

    This is a multi-phase operation:
      Phase 0: Find RECONCILE_ configlets and figure out where each device is attached
      Phase 1: Categorize each device — new assignment, update existing, or append body
      Phase 2: Find orphan/duplicate assignments to clean up
      Phase 3: Report the plan (what will be created/updated/removed)
      Phase 4: Execute — create configlets and assignments in a workspace
      Phase 5: Update parent containers with new children
      Phase 6: Remove old RECONCILE_ references from assignments
      Phase 7: Clean up orphans, update studio roots, build, and submit
    """
    if not args.device_filter:
        logger.error("--device-filter is required for the reconcile operation")
        sys.exit(1)

    # ── Phase 0a: Find RECONCILE_ configlets and filter by device name ──
    reconciled = await get_reconciled_configlets(channel)
    if not reconciled:
        logger.error("No reconciled configlets found (ID prefix RECONCILE_)")
        return

    # Filter to only the devices matching the --device-filter (substring match on name)
    device_filter = args.device_filter
    filtered = [(cid, name, dh, body) for cid, name, dh, body in reconciled if device_filter in name]
    logger.info('Filtered to %d/%d reconciled configlets matching "%s"', len(filtered), len(reconciled), device_filter)
    reconciled = filtered
    if not reconciled:
        logger.error('No reconciled configlets match filter "%s"', device_filter)
        return

    # Map device_hash -> (configlet_id, display_name, body)
    reconcile_map = {dh: (cid, name, body) for cid, name, dh, body in reconciled}

    # Fetch the current state of all assignments and studio roots
    assignments = await get_all_assignments(channel)
    current_inputs = await get_studio_inputs(channel)
    existing_roots = current_inputs.get("configletAssignmentRoots", []) if current_inputs else []

    # ── Phase 0b: Figure out where each device should be attached ──
    # Two modes:
    #   1. --assignment-root specified: attach all devices under that container
    #   2. No --assignment-root: auto-detect each device's current location in the tree
    device_to_parent = {}  # device_hash -> parent container assignment ID (or None for root)
    device_to_existing = {}  # device_hash -> (assignment_id, assignment_info) if already exists
    for device_hash, (_, reconcile_name, _) in reconcile_map.items():
        # Extract hostname from the reconcile display name ("Reconcile <hostname> <timestamp>")
        parts = reconcile_name.split(" ")
        device_name = parts[1] if len(parts) >= 2 else device_hash

        if args.assignment_root:
            # User specified exactly where to put it — use that container
            root_id = await resolve_assignment_root(assignments, args.assignment_root)
            if not root_id:
                logger.error('Assignment root "%s" not found (by ID or name)', args.assignment_root)
                return
            root = assignments[root_id]
            logger.info("Using specified assignment root: %s (%s)", root["name"], root_id)
            device_to_parent[device_hash] = root_id
        else:
            # Auto-detect: search for an existing assignment for this device
            # (checks both serial number and hostname, skips RECONCILE_TREE_KEY)
            existing_aid, parent_id = find_device_assignment(assignments, device_hash, device_name)
            if existing_aid:
                existing = assignments[existing_aid]
                device_to_existing[device_hash] = (existing_aid, existing)
                if parent_id:
                    parent = assignments[parent_id]
                    logger.info(
                        "Device %s (%s) found under container: %s (%s)",
                        device_name,
                        existing["query"],
                        parent["name"] or parent["query"],
                        parent_id,
                    )
                else:
                    logger.info(
                        "Device %s (%s) found at studio root",
                        device_name,
                        existing["query"],
                    )
                device_to_parent[device_hash] = parent_id
            else:
                # Device not found anywhere — will create a new assignment at the root level
                logger.info("Device %s not found in any assignment, will attach to studio root", device_name)
                device_to_parent[device_hash] = None

    # ── Phase 1: Categorize each device into one of three action types ──
    updates_existing = []  # Device already has an assignment — add the new configlet to it
    updates_body = []  # Device already has a "-reconciled" configlet — append new config to it
    creates_new = []  # Device has no assignment — create a brand new one
    for device_hash, (reconcile_cid, reconcile_name, body) in reconcile_map.items():
        parts = reconcile_name.split(" ")
        device_name = parts[1] if len(parts) >= 2 else device_hash

        new_configlet_name = f"{device_name}-reconciled"
        # Generate the deterministic ID for this configlet name
        expected_new_cid = str(uuid5(ID_NAMESPACE, f"configlet:{new_configlet_name}"))

        if device_hash in device_to_existing:
            # Device already has an assignment — check if a "-reconciled" configlet exists
            child_id, child = device_to_existing[device_hash]
            if expected_new_cid in child["configlet_ids"]:
                # A "-reconciled" configlet already exists — check if the body changed
                stub = configlet.ConfigletServiceStub(channel)
                req = configlet.ConfigletRequest(
                    key=configlet.ConfigletKey(
                        workspace_id=MAINLINE_WS_ID,
                        configlet_id=expected_new_cid,
                    )
                )
                resp = await stub.get_one(req, timeout=RPC_TIMEOUT)
                existing_body = resp.value.body or ""
                if body in existing_body:
                    # Body is already there (idempotent) — nothing to do
                    logger.info("Already up to date: %s -> %s", device_name, new_configlet_name)
                    continue
                # New config to append to the existing body
                logger.info(
                    "New config to append to %s (%d + %d chars)", new_configlet_name, len(existing_body), len(body)
                )
                updates_body.append((expected_new_cid, existing_body, body, device_name))
                continue
            # Assignment exists but doesn't have a "-reconciled" configlet yet — add one
            updates_existing.append((child_id, child, body, device_name, device_hash))
        else:
            # No existing assignment found — need to create one from scratch
            creates_new.append((body, device_name, device_hash))

    # ── Phase 2: Find duplicate/orphan assignments to clean up ──
    # Look for root-level assignments that ONLY contain RECONCILE_ configlets.
    # These were probably created by a previous reconcile run and are now redundant.
    all_reconcile_cids = set(cid for cid, _, _, _ in reconciled)
    used_root_ids = set(device_to_parent.values()) - {None}

    orphan_roots = []
    for aid in existing_roots:
        if aid in used_root_ids or aid == "RECONCILE_TREE_KEY":
            continue
        ainfo = assignments.get(aid)
        if not ainfo or not ainfo["configlet_ids"]:
            continue
        if all(cid in all_reconcile_cids for cid in ainfo["configlet_ids"]):
            orphan_roots.append(aid)

    # Find any existing assignments that still reference old RECONCILE_ configlet IDs
    # in their configlet list — these refs should be removed since we're creating
    # proper named configlets to replace them.
    old_reconcile_refs = []
    for parent_id in used_root_ids:
        parent = assignments.get(parent_id, {})
        for child_id in parent.get("child_assignment_ids", []):
            child = assignments.get(child_id)
            if not child:
                continue
            reconcile_refs = [cid for cid in child["configlet_ids"] if cid.startswith("RECONCILE_")]
            if reconcile_refs:
                old_reconcile_refs.append((child_id, child, reconcile_refs))

    if not updates_existing and not updates_body and not creates_new and not orphan_roots and not old_reconcile_refs:
        logger.info("Nothing to do — all reconciled configlets are already up to date")
        return

    # ── Phase 3: Report the plan before making changes ──
    if creates_new:
        logger.info("Will create %d new device assignment(s):", len(creates_new))
        for _, device_name, dh in creates_new:
            parent_id = device_to_parent[dh]
            parent_name = assignments[parent_id]["name"] if parent_id else "(studio root)"
            logger.info("  %s: create %s-reconciled under %s", device_name, device_name, parent_name)

    if updates_existing:
        logger.info("Will add to %d existing assignment(s):", len(updates_existing))
        for _, child, _, device_name, _ in updates_existing:
            logger.info("  %s: create %s-reconciled", device_name, device_name)

    if updates_body:
        logger.info("Will append to %d configlet(s):", len(updates_body))
        for _, existing_body, new_body, device_name in updates_body:
            logger.info("  %s-reconciled: %d existing + %d new chars", device_name, len(existing_body), len(new_body))

    if old_reconcile_refs:
        logger.info("Will remove %d RECONCILE_ reference(s) from assignments:", len(old_reconcile_refs))
        for child_id, child, refs in old_reconcile_refs:
            logger.info("  %s: remove %s", child["name"], refs)

    if orphan_roots:
        logger.info("Will remove %d duplicate root assignment(s):", len(orphan_roots))
        for aid in orphan_roots:
            ainfo = assignments[aid]
            logger.info("  %s (%s)", aid, ainfo["name"] or "(unnamed)")

    # ── Phase 4: Execute changes in a workspace ──
    ws_id = await create_workspace(channel, "Copy reconciled configlets")
    await asyncio.sleep(1)

    # Track which new assignments need to be added to which parent container
    new_children_by_parent = {}

    # Phase 4a: Create brand new device assignments (device wasn't in the tree before)
    for body, device_name, device_hash in creates_new:
        if not body:
            logger.warning("Skipping %s: reconciled configlet body is empty", device_name)
            continue
        new_configlet_name = f"{device_name}-reconciled"
        new_cid = await create_configlet(channel, ws_id, new_configlet_name, body)
        device_query = f"device:{device_hash}"
        aid = await create_assignment(channel, ws_id, device_hash, device_query, [new_cid])
        parent_id = device_to_parent[device_hash]
        new_children_by_parent.setdefault(parent_id, []).append(aid)
        logger.info("Created %s (%d chars) and new assignment for %s", new_configlet_name, len(body), device_name)

    # Phase 4b: Add the reconciled configlet to an existing device assignment
    for child_id, child, body, device_name, device_hash in updates_existing:
        if not body:
            logger.warning("Skipping %s: reconciled configlet body is empty", device_name)
            continue
        new_configlet_name = f"{device_name}-reconciled"
        new_cid = await create_configlet(channel, ws_id, new_configlet_name, body)
        # Keep existing non-RECONCILE configlets and add the new one
        new_configlet_ids = [cid for cid in child["configlet_ids"] if not cid.startswith("RECONCILE_")]
        new_configlet_ids.append(new_cid)
        await update_assignment_configlets(channel, ws_id, child_id, new_configlet_ids)
        logger.info("Created %s (%d chars) and assigned to %s", new_configlet_name, len(body), device_name)

    # Phase 4c: Append new config to an existing "-reconciled" configlet
    # (the device was reconciled again since the last run)
    for _, existing_body, new_body, device_name in updates_body:
        combined = existing_body + "\n" + new_body
        await create_configlet(channel, ws_id, f"{device_name}-reconciled", combined)
        logger.info("Appended to %s-reconciled (%d -> %d chars)", device_name, len(existing_body), len(combined))

    # ── Phase 5: Add new assignments to their parent containers ──
    new_root_ids = []
    for parent_id, new_child_ids in new_children_by_parent.items():
        if parent_id is None:
            # No parent — add directly to the studio's root list
            new_root_ids.extend(new_child_ids)
            logger.info("Added %d assignment(s) to studio root", len(new_child_ids))
        else:
            # Update the parent container to include the new child assignments
            parent = assignments[parent_id]
            updated_children = parent["child_assignment_ids"] + new_child_ids
            await create_container(
                channel, ws_id, parent_id, parent["name"], updated_children, configlet_ids=parent["configlet_ids"]
            )
            logger.info('Updated "%s" children: added %d assignment(s)', parent["name"], len(new_child_ids))

    # ── Phase 6: Clean up old RECONCILE_ configlet references ──
    # For assignments we already updated in Phase 4b, the RECONCILE_ refs were
    # already removed. This handles other assignments that still have stale refs.
    updated_child_ids = set(cid for cid, _, _, _, _ in updates_existing)
    for child_id, child, refs in old_reconcile_refs:
        if child_id in updated_child_ids:
            continue  # Already handled in Phase 4b
        cleaned_ids = [cid for cid in child["configlet_ids"] if not cid.startswith("RECONCILE_")]
        await update_assignment_configlets(channel, ws_id, child_id, cleaned_ids)
        logger.info("Removed RECONCILE_ refs from %s: %s", child["name"], refs)

    # ── Phase 7: Clean up orphan root assignments and update the studio roots list ──
    updated_roots = existing_roots
    if orphan_roots:
        for aid in orphan_roots:
            await delete_assignment(channel, ws_id, aid)
            logger.info("Deleted duplicate assignment: %s", aid)
        updated_roots = [r for r in updated_roots if r not in set(orphan_roots)]
    if new_root_ids:
        updated_roots = updated_roots + new_root_ids
    if orphan_roots or new_root_ids:
        await set_studio_roots(channel, ws_id, updated_roots)
        logger.info("Studio roots updated: %s", updated_roots)

    if not await build_workspace(channel, ws_id):
        return

    if args.build_only:
        logger.info("Build-only mode, stopping. Workspace ID: %s", ws_id)
        return

    cc_ids, submitted = await submit_workspace(channel, ws_id)
    if not submitted:
        return
    logger.info("%d change control(s) created: %s", len(cc_ids), cc_ids)


# ─────────────────────────────────────────────────────────────────────────────
# CLEANUP OPERATION — Remove original RECONCILE_ configlets
#
# After the "reconcile" operation has copied reconciled configs into proper
# named configlets, this operation cleans up the originals:
#   1. Deletes all RECONCILE_ configlets
#   2. Deletes assignments that only referenced RECONCILE_ configlets
#   3. Removes RECONCILE_ refs from assignments that have other configlets too
#   4. Removes RECONCILE_TREE_KEY from the studio's root list
# ─────────────────────────────────────────────────────────────────────────────


async def delete_configlet(channel, ws_id, configlet_id):
    """Delete a configlet within a workspace."""
    req = configlet.ConfigletConfigDeleteRequest(
        key=configlet.ConfigletKey(
            workspace_id=ws_id,
            configlet_id=configlet_id,
        )
    )
    stub = configlet.ConfigletConfigServiceStub(channel)
    await stub.delete(req, timeout=RPC_TIMEOUT)


async def cleanup_reconciled(channel, args):
    """Remove original RECONCILE_ configlets and their references after reconcile is done."""
    reconciled = await get_reconciled_configlets(channel)
    if not reconciled:
        logger.info("No RECONCILE_ configlets found, nothing to clean up")
        return

    assignments = await get_all_assignments(channel)

    # Categorize assignments that reference RECONCILE_ configlets:
    # - If an assignment ONLY has RECONCILE_ refs → delete the whole assignment
    # - If an assignment has a mix → just remove the RECONCILE_ refs, keep the rest
    reconcile_cids = set(cid for cid, _, _, _ in reconciled)
    assignments_to_delete = []
    assignments_to_update = []
    for aid, ainfo in assignments.items():
        reconcile_refs = [cid for cid in ainfo["configlet_ids"] if cid in reconcile_cids]
        if not reconcile_refs:
            continue
        non_reconcile_refs = [cid for cid in ainfo["configlet_ids"] if cid not in reconcile_cids]
        if non_reconcile_refs:
            assignments_to_update.append((aid, ainfo, non_reconcile_refs, reconcile_refs))
        else:
            assignments_to_delete.append((aid, ainfo))

    # RECONCILE_TREE_KEY is the special root node that CloudVision uses to organize
    # reconciled configlets. Once we've cleaned up, it should be removed from the roots.
    current_inputs = await get_studio_inputs(channel)
    existing_roots = current_inputs.get("configletAssignmentRoots", []) if current_inputs else []
    remove_tree_key = "RECONCILE_TREE_KEY" in existing_roots

    if not reconciled and not assignments_to_delete and not remove_tree_key:
        logger.info("Nothing to clean up")
        return

    # ── Report plan ──
    logger.info("Will delete %d RECONCILE_ configlet(s):", len(reconciled))
    for cid, name, _, _ in sorted(reconciled, key=lambda x: x[1]):
        logger.info("  %s (%s)", name, cid)

    if assignments_to_delete:
        logger.info(
            "Will delete %d assignment(s) that only reference RECONCILE_ configlets:", len(assignments_to_delete)
        )
        for aid, ainfo in assignments_to_delete:
            logger.info("  %s (%s)", aid, ainfo["name"] or "(unnamed)")

    if assignments_to_update:
        logger.info("Will remove RECONCILE_ refs from %d assignment(s):", len(assignments_to_update))
        for aid, ainfo, _, refs in assignments_to_update:
            logger.info("  %s: remove %s", ainfo["name"], refs)

    if remove_tree_key:
        logger.info("Will remove RECONCILE_TREE_KEY from configletAssignmentRoots")

    ws_id = await create_workspace(channel, "Clean up reconciled configlets")
    await asyncio.sleep(1)

    # ── Delete RECONCILE_ configlets ──
    for cid, name, _, _ in reconciled:
        await delete_configlet(channel, ws_id, cid)
        logger.info("Deleted configlet: %s (%s)", name, cid)

    # ── Delete assignments that only had RECONCILE_ refs ──
    for aid, ainfo in assignments_to_delete:
        await delete_assignment(channel, ws_id, aid)
        logger.info("Deleted assignment: %s (%s)", aid, ainfo["name"] or "(unnamed)")

    # ── Update assignments that had mixed refs ──
    for aid, ainfo, keep_ids, _ in assignments_to_update:
        await update_assignment_configlets(channel, ws_id, aid, keep_ids)
        logger.info("Updated assignment %s (%s): %s", aid, ainfo["name"], keep_ids)

    # ── Remove RECONCILE_TREE_KEY from roots ──
    if remove_tree_key:
        cleaned_roots = [r for r in existing_roots if r != "RECONCILE_TREE_KEY"]
        # Also remove any deleted assignments from roots
        deleted_aids = set(aid for aid, _ in assignments_to_delete)
        cleaned_roots = [r for r in cleaned_roots if r not in deleted_aids]
        await set_studio_roots(channel, ws_id, cleaned_roots)
        logger.info("Studio roots updated: %s", cleaned_roots)

    if not await build_workspace(channel, ws_id):
        return

    if args.build_only:
        logger.info("Build-only mode, stopping. Workspace ID: %s", ws_id)
        return

    cc_ids, submitted = await submit_workspace(channel, ws_id)
    if not submitted:
        return
    logger.info("%d change control(s) created: %s", len(cc_ids), cc_ids)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN — Route to the appropriate operation and handle filtering/output
# ─────────────────────────────────────────────────────────────────────────────


async def main(args, client):
    with client as channel:
        # ── GET operation ──
        # Read the current configlet tree from CloudVision and output as YAML.
        # Supports filtering by device hostname and showing tag-matched containers.
        if args.operation == "get":
            inventory = await get_inventory(channel, include_reconcile=args.include_reconcile)

            if args.device_filter:
                # The user wants to see only configlets related to specific device(s).
                # Step 1: Convert the hostname filter to device serial numbers.
                matching_ids = await resolve_hostname_filter(channel, args.device_filter)
                if not matching_ids:
                    logger.error('No devices found with hostname matching "%s"', args.device_filter)
                    return
                logger.info(
                    'Resolved hostname filter "%s" to %d device(s): %s',
                    args.device_filter,
                    len(matching_ids),
                    ", ".join(sorted(matching_ids)),
                )
                # Step 2: Filter devices to only those matching our filter.
                # Include device:* entries (they apply to all devices) and handle
                # comma-separated multi-device entries. Exclude devices under the
                # "Reconciled Configlets" container (shown separately in reconcile section).
                devices = inventory.get("devices", [])
                if args.include_tag_matches:
                    # With --include-tag-matches, include device:* entries (they apply to all devices)
                    devices = [
                        d
                        for d in devices
                        if d.get("container") != "Reconciled Configlets"
                        and (
                            d["device_id"] == "*"
                            or d["device_id"] in matching_ids
                            or any(did in matching_ids for did in d["device_id"].split(","))
                        )
                    ]
                else:
                    # Without --include-tag-matches, exclude device:* entries
                    devices = [
                        d
                        for d in devices
                        if d.get("container") != "Reconciled Configlets"
                        and d["device_id"] != "*"
                        and (
                            d["device_id"] in matching_ids
                            or any(did in matching_ids for did in d["device_id"].split(","))
                        )
                    ]
                containers = inventory.get("containers", [])
                containers = [c for c in containers if c.get("name") != "Reconciled Configlets"]

                # Step 3: Filter containers.
                if args.include_tag_matches:
                    # --include-tag-matches: show containers whose tag query would
                    # match the device based on its actual CloudVision tags.
                    # First, fetch all tags for each matching device.
                    all_device_tags = set()
                    for did in matching_ids:
                        device_tags = await get_device_tags(channel, did)
                        logger.info("Tags for %s: %s", did, device_tags)
                        all_device_tags.update(device_tags)
                    # Then filter containers to those whose query matches the device's tags
                    containers = [
                        c for c in containers if _query_matches_tags(c.get("query", ""), all_device_tags, matching_ids)
                    ]
                else:
                    # Without --include-tag-matches: only show containers that are
                    # direct ancestors of the filtered devices in the tree path.
                    kept_containers = set()
                    for d in devices:
                        if "container" in d:
                            # Keep all ancestor containers (e.g., "US/DC1" keeps "US" and "US/DC1")
                            parts = d["container"].split("/")
                            for i in range(len(parts)):
                                kept_containers.add("/".join(parts[: i + 1]))
                    containers = [c for c in containers if c["name"] in kept_containers]

                # Step 4: Filter reconcile entries to matching devices
                reconcile = inventory.get("reconcile", [])
                reconcile = [
                    r
                    for r in reconcile
                    if r["device_id"] in matching_ids or args.device_filter in r.get("hostname", "")
                ]
                inventory = {}
                if containers:
                    inventory["containers"] = containers
                if devices:
                    inventory["devices"] = devices
                if reconcile:
                    inventory["reconcile"] = reconcile
                logger.info(
                    "Filtered to %d device(s), %d container(s), and %d reconcile configlet(s)",
                    len(devices),
                    len(containers),
                    len(reconcile),
                )

            # --save-configlets: write each configlet body to a file and remove
            # the body from the YAML output so it stays compact.
            if args.save_configlets:
                import os

                os.makedirs(args.configlet_dir, exist_ok=True)
                saved_count = 0
                for section in ("containers", "devices", "reconcile"):
                    for entry in inventory.get(section, []):
                        for cfg in entry.get("configlets", []):
                            body = cfg.get("body", "")
                            if body and body != "(empty)":
                                filename = f"{cfg['name']}.cfg"
                                filepath = os.path.join(args.configlet_dir, filename)
                                with open(filepath, "w", encoding="utf-8") as f:
                                    f.write(body)
                                cfg["configlet_file"] = filepath
                                saved_count += 1
                            del cfg["body"]
                        if section == "reconcile" and "body" in entry:
                            body = entry["body"]
                            if body and body != "(empty)":
                                filename = f"{entry['display_name']}.cfg"
                                filepath = os.path.join(args.configlet_dir, filename)
                                with open(filepath, "w", encoding="utf-8") as f:
                                    f.write(body)
                                saved_count += 1
                            del entry["body"]
                logger.info("Saved %d configlet(s) to %s/", saved_count, args.configlet_dir)

            output = yaml.dump(inventory, default_flow_style=False, sort_keys=False)
            print(output)

            if args.output_file:
                with open(args.output_file, "w", encoding="utf-8") as f:
                    f.write(output)
                logger.info("Inventory written to %s", args.output_file)
            return

        if args.operation == "reconcile":
            await reconcile_configlets(channel, args)
            return

        if args.operation == "cleanup-reconciled":
            await cleanup_reconciled(channel, args)
            return

        # ── SET operation ──
        # Push configlets from an inventory (YAML file or hardcoded default) to CloudVision.
        # This creates a workspace, sets up configlets/assignments/containers, builds,
        # and submits — resulting in Change Controls for each affected device.
        if args.inventory_file:
            with open(args.inventory_file, encoding="utf-8") as f:
                inventory = yaml.safe_load(f)
            logger.info("Loaded inventory from %s", args.inventory_file)
        else:
            inventory = INVENTORY

        devices = inventory.get("devices", [])
        containers = inventory.get("containers", [])
        if not devices and not containers:
            logger.error("INVENTORY is empty, nothing to do")
            sys.exit(1)

        # Validate the inventory before creating a workspace.
        # Fetch existing configlets so we can check name-only entries exist on CloudVision.
        existing_configlets = await get_configlet_name_to_id(channel)
        errors = validate_inventory(inventory, existing_configlets)
        if errors:
            for err in errors:
                logger.error(err)
            sys.exit(1)

        ws_parts = []
        if devices:
            ws_parts.append(", ".join(d["device_id"] for d in devices))
        if containers:
            ws_parts.append(", ".join(c["name"] for c in containers))
        ws_name = f"Assign configlets to {'; '.join(ws_parts)}"
        ws_id = await create_workspace(channel, ws_name)
        await asyncio.sleep(1)

        # ── Build the container tree from INVENTORY paths ──
        #
        # Each node in the tree tracks its own child container nodes,
        # the device assignment IDs that land directly on it, and
        # configlet IDs assigned to the container itself.
        # A path like "DC1/France" produces two nodes:
        #   ""     (virtual root)  ->  children: {"DC1": node}
        #   "DC1"                  ->  children: {"France": node}
        #   "DC1/France"           ->  device_assignment_ids: [...]
        #
        # After all devices are processed the tree is walked bottom-up
        # so that each container's child_assignment_ids are known before
        # its own ConfigletAssignment is created on CloudVision.

        def _container_id_for_path(path):
            return str(uuid5(ID_NAMESPACE, f"container:{path}"))

        tree = {}  # path -> {"children": {}, "device_ids": [], "configlet_ids": []}
        root_children = {}  # name -> path   (top-level containers)

        def _ensure_path(path):
            if path in tree:
                return
            tree[path] = {"children": {}, "device_ids": [], "configlet_ids": []}
            parts = path.split("/")
            if len(parts) == 1:
                root_children[parts[0]] = path
                return
            parent_path = "/".join(parts[:-1])
            _ensure_path(parent_path)
            tree[parent_path]["children"][parts[-1]] = path

        root_assignment_ids = []

        # ── Create / resolve configlets for containers ──
        for container in containers:
            container_path = container["name"]
            _ensure_path(container_path)
            for entry in container.get("configlets", []):
                cid = await resolve_configlet(channel, ws_id, entry, existing_configlets)
                tree[container_path]["configlet_ids"].append(cid)

        # ── Create / resolve configlets and assignments for devices ──
        for device in devices:
            device_id = device["device_id"]
            device_query = f"device:{device_id}"

            configlet_ids = []
            for entry in device["configlets"]:
                cid = await resolve_configlet(channel, ws_id, entry, existing_configlets)
                configlet_ids.append(cid)

            assignment_id = await create_assignment(channel, ws_id, device_id, device_query, configlet_ids)

            if "container" in device:
                container_path = device["container"]
                _ensure_path(container_path)
                tree[container_path]["device_ids"].append(assignment_id)
            else:
                root_assignment_ids.append(assignment_id)

        # ── Assign container location tags to devices ──
        # Each segment of a device's container path becomes a
        # location:<segment> tag on that device, so the container
        # queries (location:<name>) match correctly.
        created_tags = set()
        for device in devices:
            if "container" not in device:
                continue
            parts = device["container"].split("/")
            for part in parts:
                await create_tag_if_needed(channel, ws_id, "location", part, created_tags)
                await assign_tag_to_device(channel, ws_id, device["device_id"], "location", part)

        # Walk every path bottom-up (longest paths first) so children
        # are created before their parents.
        for path in sorted(tree, key=lambda p: p.count("/"), reverse=True):
            node = tree[path]
            child_assignment_ids = [_container_id_for_path(cp) for cp in node["children"].values()] + node["device_ids"]
            display_name = path.split("/")[-1]
            await create_container(
                channel,
                ws_id,
                _container_id_for_path(path),
                display_name,
                child_assignment_ids,
                configlet_ids=node["configlet_ids"],
            )

        # Only true roots go into configletAssignmentRoots.
        for path in root_children.values():
            root_assignment_ids.append(_container_id_for_path(path))

        await update_studio_roots(channel, ws_id, root_assignment_ids)

        if not await build_workspace(channel, ws_id):
            return

        if args.build_only:
            logger.info("Build-only mode, stopping here. Workspace ID: %s", ws_id)
            return

        cc_ids, submitted = await submit_workspace(channel, ws_id)
        if not submitted:
            return
        logger.info("%d change control(s) created: %s", len(cc_ids), cc_ids)


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND-LINE INTERFACE
#
# Required for all operations:
#   --server <host:port>   CloudVision server address
#   --token-file <path>    Service account token file
#
# Optional connection:
#   --insecure             Skip TLS certificate verification
#   --cert-file <path>     CA certificate file for TLS
#
# Operations:
#   --operation get        Read and display the configlet tree (default)
#   --operation set        Push configlets from INVENTORY or --inventory-file
#   --operation reconcile  Copy RECONCILE_ configlets into named configlets
#   --operation cleanup-reconciled  Delete original RECONCILE_ configlets
#
# Filtering (get/reconcile):
#   --device-filter <str>  Filter by hostname substring
#   --include-tag-matches  Show containers matching device tags (get only)
#
# Output:
#   --output-file <path>   Save get output to YAML file
#   --inventory-file <path> Read inventory from YAML file (set only)
#   --build-only           Preview changes without submitting
#   --debug                Write debug JSON files and enable verbose logging
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Assign static configlets to devices via CVP Studio API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # List existing configlets and assignments:\n"
            "  python3 assign_static_configlet.py \\\n"
            "      --server 192.0.2.10:443 --token-file token.tok --insecure \\\n"
            "      --operation get\n\n"
            "  # Assign configlets defined in INVENTORY:\n"
            "  python3 assign_static_configlet.py \\\n"
            "      --server 192.0.2.10:443 --token-file token.tok --insecure \\\n"
            "      --operation set\n"
        ),
    )
    parser.add_argument("--server", required=True, help="CVP server in <host>:<port> format")
    parser.add_argument(
        "--token-file", required=True, type=argparse.FileType("r"), help="File containing the service account token"
    )
    parser.add_argument("--cert-file", type=str, default=None, help="Path to CA certificate file")
    parser.add_argument("--insecure", action="store_true", default=False, help="Skip TLS certificate verification")
    parser.add_argument(
        "--operation",
        choices=["get", "set", "reconcile", "cleanup-reconciled"],
        default="get",
        help="get: list configlets/assignments; set: assign configlets; "
        "reconcile: copy reconciled configlets and assign to devices; "
        "cleanup-reconciled: delete original RECONCILE_ configlets "
        "and remove RECONCILE_TREE_KEY",
    )
    parser.add_argument(
        "--assignment-root",
        default=None,
        help="Assignment root ID or name to add reconciled configlets under. "
        "If not specified, attaches to the device's current container "
        "or to the studio root if the device is not in any container.",
    )
    parser.add_argument(
        "--device-filter",
        default=None,
        help="Filter by substring match on device hostname. "
        "For get: resolves matching hostnames to device IDs and filters results. "
        "For reconcile: matches configlet name. "
        "Required for --operation reconcile.",
    )
    parser.add_argument(
        "--output-file",
        default=None,
        help="Save get output to this YAML file",
    )
    parser.add_argument(
        "--save-configlets",
        action="store_true",
        default=False,
        help="Save each configlet body to a separate .cfg file and omit bodies from the YAML output",
    )
    parser.add_argument(
        "--configlet-dir",
        default="configlets",
        help="Directory to save configlet files when using --save-configlets (default: configlets)",
    )
    parser.add_argument(
        "--inventory-file",
        default=None,
        help="YAML file containing inventory dict for the set operation (overrides hardcoded INVENTORY)",
    )
    parser.add_argument(
        "--include-tag-matches",
        action="store_true",
        default=False,
        help="With --device-filter: also show containers whose tag query "
        "matches the filtered device(s) based on their CloudVision tags",
    )
    parser.add_argument(
        "--include-reconcile",
        action="store_true",
        default=False,
        help="Include reconcile configlets in the get output",
    )
    parser.add_argument("--build-only", action="store_true", default=False, help="Stop after building (no submission)")
    parser.add_argument(
        "--debug", action="store_true", default=False, help="Enable debug logging and write debug files"
    )

    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    client = create_client(args)
    asyncio.run(main(args, client))
