#!/usr/bin/env python3
"""Migrate profiles from Interface Configuration Studio to Data Center
Interface Configuration Studio in Arista CloudVision-as-a-Service.

Reads all profiles and their port assignments from the Interface Configuration
Studio (ICS), maps them to the Data Center Interface Configuration Studio
(DICS) schema, and writes them within a workspace.  Optionally submits the
workspace and the resulting change control.

The two studios have completely different schemas:
  - ICS root:  profiles (collection), devices (resolver), globalSettings
  - DICS root: portProfiles (collection), networkResolver (DC hierarchy),
               spineNetworkResolver, advancedSettings

This script handles the field-level mapping between the two, reads device
tags to place interfaces in the correct DICS DC/Pod/Domain hierarchy, and
optionally detaches migrated assignments from the ICS.
"""

import argparse
import asyncio
import json
import sys
import uuid

from grpclib.exceptions import GRPCError
from grpclib.const import Status as GRPCStatus

from cloudvision.api.client import AsyncCVClient
from cloudvision.api.fmp import RepeatedString

# --- Studio APIs (aristaproto wrappers) ---
# These provide read/write access to studio inputs, schemas, and assigned-tags.
from cloudvision.api.arista.studio.v1 import (
    AssignedTags,
    AssignedTagsConfig,
    AssignedTagsConfigServiceStub,
    AssignedTagsConfigSetRequest,
    AssignedTagsServiceStub,
    AssignedTagsStreamRequest,
    InputFieldType,
    Inputs,
    InputsConfig,
    InputsConfigServiceStub,
    InputsConfigSetRequest,
    InputsKey,
    InputsServiceStub,
    InputsStreamRequest,
    StudioKey,
    StudioRequest,
    StudioServiceStub,
    StudioSummaryServiceStub,
    StudioSummaryStreamRequest,
)

# --- Workspace APIs ---
# Workspace lifecycle: create -> build -> submit -> (change control).
from cloudvision.api.arista.workspace.v1 import (
    BuildState,
    Request,
    RequestParams,
    WorkspaceBuildKey,
    WorkspaceBuildServiceStub,
    WorkspaceBuildRequest,
    WorkspaceConfig,
    WorkspaceConfigServiceStub,
    WorkspaceConfigSetRequest,
    WorkspaceKey,
    WorkspaceServiceStub,
    WorkspaceRequest,
    WorkspaceState,
)

# --- Change Control APIs ---
# Used to approve and execute a change control after workspace submission.
from cloudvision.api.arista.changecontrol.v1 import (
    ApproveConfig,
    ApproveConfigServiceStub,
    ApproveConfigSetRequest,
    ChangeControlConfig,
    ChangeControlConfigServiceStub,
    ChangeControlConfigSetRequest,
    ChangeControlKey,
    ChangeControlServiceStub,
    ChangeControlRequest,
    FlagConfig,
)

# --- Tag APIs ---
# Tags identify which DC, DC-Pod, and Leaf-Domain a device belongs to.
# This is required for placing interfaces in the DICS hierarchy.
from cloudvision.api.arista.tag.v2 import (
    ElementType,
    TagAssignment,
    TagAssignmentKey,
    TagAssignmentServiceStub,
    TagAssignmentStreamRequest,
)

# ---- Constants ----

# Display names used to discover studio IDs at runtime.
ICS_DISPLAY_NAME = "Interface Configuration"
DICS_DISPLAY_NAME = "Data Center Interface Configuration"

# An empty workspace_id refers to committed mainline state.
MAINLINE_WS_ID = ""

# Seconds between workspace build / submit polling attempts.
BUILD_POLL_INTERVAL = 5

# Retry parameters for workspace writes.  After workspace creation, CVaaS
# may return UNAVAILABLE or NOT_FOUND for a brief window before the
# workspace is fully registered.
WS_RETRY_INTERVAL = 2
WS_RETRY_MAX = 30
RETRYABLE_STATUSES = {GRPCStatus.UNAVAILABLE, GRPCStatus.NOT_FOUND}

# Human-readable names for InputFieldType enum values (used in discover mode).
FIELD_TYPE_NAMES = {t: t.name for t in InputFieldType}

# ---- Field Mapping Tables ----
#
# The ICS and DICS store profile data as flat JSON dicts, but the field names
# differ.  The DICS uses short names (e.g. "name", "mode", "vlans") rather
# than the long schema field IDs (e.g. "portProfileName", "portProfileMode").
# Similarly, sub-group members strip their parent prefix:
#   portProfilePortChannelMlag  ->  mlag   (inside the "portChannel" group)

# ICS profile field -> DICS port profile field (direct 1:1 mappings).
# The "mode" and "speed" fields are handled separately because they
# require value translation (ICS dropdown options -> DICS/EOS values).
PROFILE_FIELD_MAP = {
    "name": "name",
    "profileDescription": "description",
}

# ICS switchport mode -> DICS mode.  The DICS uses "trunk phone" instead
# of "phone", and "routed" has no DICS equivalent (handled via eosCli).
ICS_MODE_MAP = {
    "access": "access",
    "trunk": "trunk",
    "phone": "trunk phone",
    "dot1q-tunnel": "dot1q-tunnel",
}

# ICS speed dropdown values -> EOS CLI "speed <value>" arguments.
# The ICS uses its own shorthand (e.g. "1gfull", "10gfull"), while the
# DICS feeds the value directly into the EOS "speed" command which uses
# a different shorthand (e.g. "1g", "10g", "100mfull").
ICS_SPEED_MAP = {
    "auto": "auto",
    "10half": "10mhalf",
    "10full": "10mfull",
    "100half": "100mhalf",
    "100full": "100mfull",
    "1000full": "1g",
    "1gfull": "1g",
    "2500full": "2.5g",
    "2.5gfull": "2.5g",
    "5000full": "5g",
    "5gfull": "5g",
    "10000full": "10g",
    "10gfull": "10g",
    "25000full": "25g",
    "25gfull": "25g",
    "40000full": "40g-4",
    "40gfull": "40g-4",
    "50000full": "50g-1",
    "50gfull": "50g-1",
    "100000full": "100g-4",
    "100gfull": "100g-4",
}

# ICS VLAN fields -> DICS "vlans" sub-group members.
VLAN_FIELD_MAP = {
    "nativeVlanId": "nativeVlan",
    "phoneVlanId": "phoneVlan",
    "allowedVlans": "vlans",
}

# The DICS hierarchy contains interface resolver arrays at several nesting
# levels.  Each uses a different adapter-details group name but the port
# profile reference field is always "portProfile".
DICS_INTF_ADAPTER_MAP = {
    # Leaf-Domain path:  ...leafDomain[]/accessPodDetails/interfaces[]
    "interfaces": ("adapterDetails", "portProfile"),
    # L2-Leaf-Domain path
    "l2LeafDomainInterfaces": (
        "l2LeafDomainAdapterDetails", "portProfile",
    ),
    # Spine path
    "spineInterfaces": ("spineAdapterDetails", "portProfile"),
}

# Device tag labels used by the DICS hierarchy resolvers.
HIERARCHY_TAG_LABELS = {"DC", "DC-Pod", "Leaf-Domain"}

# Module-level flag set by --debug; controls verbose output.
DEBUG = False


def debug(msg):
    """Print a message only when --debug is enabled."""
    if DEBUG:
        print(f"  [DEBUG] {msg}")


# ---------------------------------------------------------------------------
# ICS data parsing
# ---------------------------------------------------------------------------

def parse_ics_data(ics_json_str):
    """Parse the ICS root input JSON and extract profiles and assignments.

    The ICS stores its data as a single JSON blob at the root input path.
    The top-level keys are "profiles" (a collection of flat dicts) and
    "devices" (a resolver array with nested interface resolver entries).

    Returns:
        profiles: dict mapping profile name -> ICS profile field dict
        assignments: dict mapping interface tag query -> profile name
    """
    data = json.loads(ics_json_str)

    # --- Extract profile definitions ---
    # ICS profile entries are flat dicts (no "inputs" wrapper), keyed by
    # the "name" field.  Example:
    #   {"name": "VLAN_50", "mode": "access", "accessVlanId": 50, ...}
    profiles = {}
    for entry in data.get("profiles", []):
        # Handle both flat format and the wrapped {"inputs": {...}} format
        # in case the studio version differs.
        if "inputs" in entry and isinstance(entry["inputs"], dict):
            fields = entry["inputs"]
        else:
            fields = entry
        name = fields.get("name") or fields.get("profileName")
        if name:
            profiles[name] = fields

    # --- Extract interface-to-profile assignments ---
    # The ICS "devices" array contains resolver entries, each selecting a
    # device via a tag query (e.g. "device:MySwitch").  Inside each device
    # is an "interface" resolver array selecting interfaces via tag queries
    # (e.g. "interface:Ethernet1@MySwitch").  Each interface entry has an
    # "intfConfig" group with a "profile" field referencing a profile by name.
    assignments = {}
    for device in data.get("devices", []):
        dev_inputs = device.get("inputs", {})
        for intf in dev_inputs.get("interface", []):
            tag_query = (intf.get("tags") or {}).get("query", "")
            config = intf.get("inputs", {}).get("intfConfig", {})
            profile_name = config.get("profile")
            if tag_query and profile_name:
                assignments[tag_query] = profile_name

    return profiles, assignments


# ---------------------------------------------------------------------------
# ICS -> DICS profile mapping
# ---------------------------------------------------------------------------

def map_profile_to_dics(ics_profile):
    """Convert a single ICS profile dict to a DICS port profile dict.

    The DICS port profile uses short JSON field names (matching the field's
    ``name`` attribute, not the long schema field ID).  For example, the
    schema field "portProfileName" maps to JSON key "name".

    Boolean-like STRING fields in the DICS use option values like "Yes"/"No"
    or EOS keywords like "edge"/"network", not Python booleans.
    """
    dics = {}

    # Simple 1:1 field copies (name, description, speed).
    for ics_key, dics_key in PROFILE_FIELD_MAP.items():
        val = ics_profile.get(ics_key)
        if val is not None:
            dics[dics_key] = val

    # Switchport mode — the ICS supports "routed" and "phone" modes that
    # are not valid DICS port profile options.  "phone" is mapped to
    # "access" (phone VLAN is handled via the vlans group).  "routed"
    # has no DICS equivalent mode, so we use the EOS CLI field to inject
    # "no switchport" and the IP address configuration directly.
    ics_mode = ics_profile.get("mode")
    if ics_mode:
        dics_mode = ICS_MODE_MAP.get(ics_mode)
        if dics_mode:
            dics["mode"] = dics_mode
        elif ics_mode == "routed":
            # Routed ports need "no switchport" and optionally an IP address.
            # Since there's no DICS mode for this, we put raw EOS commands
            # into the eosCli field which the DICS appends to the interface.
            eos_lines = ["no switchport"]
            ip_addr = ics_profile.get("ipaddress")
            if ip_addr:
                eos_lines.append(f"ip address {ip_addr}")
            dics["eosCli"] = "\n".join(eos_lines)
            debug(f"Routed profile '{ics_profile.get('name')}': "
                  f"eosCli={dics['eosCli']!r}")
        else:
            profile_name = ics_profile.get("name", "?")
            print(f"  WARNING: profile '{profile_name}' has unsupported "
                  f"mode '{ics_mode}' — skipping mode field")

    # Phone trunk — when the ICS mode is "phone", the DICS "trunk phone"
    # mode also needs the phone section's trunk field set to "tagged" to
    # indicate the phone VLAN is carried as a tagged VLAN on the port.
    if ics_mode == "phone":
        dics["phone"] = {"trunk": "tagged"}

    # Interface speed — the ICS uses dropdown shorthand values like "1gfull"
    # which need to be translated to EOS CLI format ("1000full") for the DICS.
    ics_speed = ics_profile.get("speed")
    if ics_speed:
        dics_speed = ICS_SPEED_MAP.get(ics_speed)
        if dics_speed:
            dics["speed"] = dics_speed
        else:
            # Pass through unrecognized values as-is; the DICS build will
            # validate them against EOS.
            dics["speed"] = ics_speed
            debug(f"Speed '{ics_speed}' not in mapping table, "
                  f"passing through as-is")

    # VLANs group — the DICS nests VLAN settings under a "vlans" dict.
    # The ICS "accessVlanId" maps to "vlans.vlans" (as a string) when in
    # access mode; "allowedVlans" takes precedence for trunk mode.
    vlans = {}
    for ics_key, dics_key in VLAN_FIELD_MAP.items():
        val = ics_profile.get(ics_key)
        if val is not None:
            vlans[dics_key] = val
    access_vlan = ics_profile.get("accessVlanId")
    if access_vlan is not None and "vlans" not in vlans:
        vlans["vlans"] = str(access_vlan)
    if vlans:
        dics["vlans"] = vlans

    # Spanning tree portfast — the ICS stores a boolean checkbox, but the
    # DICS expects the EOS keyword ("edge" or "network").  The ICS defaults
    # portFastEnabled to True (enabled), so a None/missing value is treated
    # as enabled.  Only an explicit False means portfast is disabled.
    portfast = ics_profile.get("portFastEnabled")
    if portfast is not False:
        dics["spanningTree"] = {"portfast": "edge"}

    # MTU
    mtu = ics_profile.get("ipmtu")
    if mtu is not None:
        dics["mtu"] = mtu

    # Port channel / MLAG — the DICS port channel group requires "enabled"
    # to be "Yes" before other settings take effect.  The DICS has a "mode"
    # field (active/on/passive) for LACP negotiation mode.
    has_port_channel = False
    pc = {}

    ch_group = (ics_profile.get("channelGroup")
                or ics_profile.get("profileChannelGroup"))
    mlag = (ics_profile.get("mlagEnabled")
            or ics_profile.get("profileMlagEnabled"))
    lacp_enabled = (ics_profile.get("lacpEnabled")
                    or ics_profile.get("profileLACPEnabled"))

    # If any port channel setting is present, enable the port channel.
    # The DICS has two enable toggles: "portChannel" (makes the section
    # visible in the studio) and "portChannelEnabled" (enables the config).
    if ch_group is not None or mlag or lacp_enabled:
        has_port_channel = True
        pc["portChannel"] = "Yes"
        pc["portChannelEnabled"] = "Yes"

    if mlag:
        pc["mlag"] = "Yes" if mlag else "No"

    # LACP negotiation mode — "active" when LACP is enabled, "on" otherwise.
    if lacp_enabled:
        pc["portChannelMode"] = "active"
    elif ch_group is not None:
        pc["portChannelMode"] = "on"

    # LACP fallback settings (if configured in the ICS profile).
    lacp_config = ics_profile.get("profileLACPConfiguration")
    if isinstance(lacp_config, dict):
        fb = {}
        fb_mode = lacp_config.get("profileLACPFallbackMode")
        if fb_mode is not None:
            fb["mode"] = fb_mode
        fb_timeout = lacp_config.get("profileLACPFallbackTimeout")
        if fb_timeout is not None:
            fb["timeout"] = fb_timeout
        if fb:
            pc["lacpFallback"] = fb

    if has_port_channel:
        dics["portChannel"] = pc
        debug(f"Port channel for '{ics_profile.get('name')}': {pc}")

    return dics


def build_dics_port_profiles(ics_profiles):
    """Convert all ICS profiles to a DICS portProfiles collection array.

    Each entry is a flat dict (no "inputs" wrapper) matching the format
    used by existing DICS profiles.
    """
    entries = []
    for ics_profile in ics_profiles.values():
        entries.append(map_profile_to_dics(ics_profile))
    return entries


# ---------------------------------------------------------------------------
# Device tag lookup
# ---------------------------------------------------------------------------

async def read_device_tags(channel, needed_devices):
    """Read device tag assignments and build a hierarchy lookup.

    The DICS organizes interfaces under DC -> DC-Pod -> Leaf-Domain.  To
    place an interface in the correct location, we need to know which DC,
    DC-Pod, and Leaf-Domain tags are assigned to its parent device.

    This function reads all device tag assignments from mainline (without
    filtering by workspace, since an empty-string workspace_id filter can
    behave differently from an unset filter in aristaproto).  It then maps
    each device's "device" tag value to its DC/Pod/Domain tags.

    Returns:
        dict: {device_tag_value: {"DC": v, "DC-Pod": v, "Leaf-Domain": v}}
    """
    service = TagAssignmentServiceStub(channel)
    filter_msg = TagAssignment(
        key=TagAssignmentKey(element_type=ElementType.DEVICE)
    )
    request = TagAssignmentStreamRequest(partial_eq_filter=[filter_msg])

    # Collect all tags per device_id (serial number).
    tags_by_device = {}
    tag_count = 0
    async for item in service.get_all(request):
        key = item.value.key
        if key.device_id and key.label and key.value:
            tags_by_device.setdefault(key.device_id, {}).setdefault(
                key.label, []
            ).append(key.value)
            tag_count += 1

    debug(f"Read {tag_count} tag assignments across "
          f"{len(tags_by_device)} devices")

    # Build device_name -> device_id mapping via the "device" tag label.
    # Also try matching device_id directly in case it IS the hostname.
    name_to_id = {}
    for device_id, tags in tags_by_device.items():
        for name in tags.get("device", []):
            name_to_id[name] = device_id
        if device_id in needed_devices:
            name_to_id[device_id] = device_id

    # Resolve hierarchy tags (DC, DC-Pod, Leaf-Domain) for each device name.
    hierarchy = {}
    for name, device_id in name_to_id.items():
        tags = tags_by_device.get(device_id, {})
        hierarchy[name] = {}
        for label in HIERARCHY_TAG_LABELS:
            values = tags.get(label, [])
            hierarchy[name][label] = values[0] if values else None

    # Attempt case-insensitive fallback for unmatched device names.
    for dev_name in needed_devices:
        if dev_name not in hierarchy:
            for known_name in name_to_id:
                if known_name.lower() == dev_name.lower():
                    hierarchy[dev_name] = hierarchy.get(known_name, {})
                    debug(f"Matched '{dev_name}' to '{known_name}' "
                          f"(case-insensitive)")
                    break
            if dev_name not in hierarchy:
                debug(f"'{dev_name}' not found in device tags")
                if dev_name in tags_by_device:
                    labels = list(tags_by_device[dev_name].keys())
                    debug(f"  Found as device_id with labels: {labels}")

    return hierarchy


# ---------------------------------------------------------------------------
# DICS interface assignment via hierarchy creation
# ---------------------------------------------------------------------------

def _find_or_create_entry(array, tag_query):
    """Find a resolver entry by tag query, or append a new empty one."""
    for entry in array:
        if isinstance(entry, dict):
            if (entry.get("tags") or {}).get("query") == tag_query:
                return entry
    new_entry = {"inputs": {}, "tags": {"query": tag_query}}
    array.append(new_entry)
    return new_entry


def apply_assignments_to_dics(dics_data, assignments, device_hierarchy):
    """Place interface-to-profile assignments in the DICS hierarchy.

    For each ICS interface assignment:
      1. Checks if the interface already exists in the DICS JSON tree
         (first pass — updates existing entries).
      2. If not found, looks up the device's DC / DC-Pod / Leaf-Domain tags
         and creates the full hierarchy path:
           dc[] -> networkDetails.dcPod[] -> networkPodDetails.leafDomain[]
             -> accessPodDetails.interfaces[]
         then sets "adapterDetails.portProfile" on the interface entry.

    Returns the set of interface tag queries that were successfully placed.
    """
    placed = set()

    # First pass: update any interfaces that already exist in the DICS tree.
    _walk_existing_entries(dics_data, assignments, placed)

    # Second pass: create hierarchy entries for the remaining assignments.
    dc_array = dics_data.setdefault("dc", [])

    for intf_query, profile_name in assignments.items():
        if intf_query in placed:
            continue

        # Parse the device name from the interface tag query.
        # Format: "interface:Ethernet6@3Site-C-SLEAF5"
        tag_value = intf_query.split(":", 1)[1] if ":" in intf_query else ""
        device_name = tag_value.split("@", 1)[1] if "@" in tag_value else ""
        if not device_name:
            print(f"    WARNING: cannot parse device from {intf_query}")
            continue

        # Look up the device's DC/Pod/Domain tags to determine where in the
        # DICS hierarchy this interface belongs.
        hier = device_hierarchy.get(device_name)
        if not hier:
            print(f"    WARNING: no tags found for device '{device_name}'")
            continue

        dc_val = hier.get("DC")
        pod_val = hier.get("DC-Pod")
        domain_val = hier.get("Leaf-Domain")
        missing = [k for k in ("DC", "DC-Pod", "Leaf-Domain")
                   if not hier.get(k)]
        if missing:
            print(f"    WARNING: device '{device_name}' missing tags: "
                  f"{', '.join(missing)}")
            continue

        # Navigate (or create) the DICS hierarchy path:
        #   DC -> DC-Pod -> Leaf-Domain -> interface
        dc = _find_or_create_entry(dc_array, f"DC:{dc_val}")
        nd = dc.setdefault("inputs", {}).setdefault("networkDetails", {})
        pod = _find_or_create_entry(
            nd.setdefault("dcPod", []), f"DC-Pod:{pod_val}"
        )
        npd = pod.setdefault("inputs", {}).setdefault("networkPodDetails", {})
        domain = _find_or_create_entry(
            npd.setdefault("leafDomain", []), f"Leaf-Domain:{domain_val}"
        )
        apd = domain.setdefault("inputs", {}).setdefault(
            "accessPodDetails", {}
        )
        intf = _find_or_create_entry(
            apd.setdefault("interfaces", []), intf_query
        )

        # Set the port profile reference on the interface's adapter details.
        adapter = intf.setdefault("inputs", {}).setdefault(
            "adapterDetails", {}
        )
        adapter["portProfile"] = profile_name
        placed.add(intf_query)

        debug(f"Created hierarchy path for {intf_query} "
              f"(DC={dc_val}, DC-Pod={pod_val}, Leaf-Domain={domain_val})")

    return placed


def _walk_existing_entries(obj, assignments, placed, array_key=None):
    """Recursively walk the DICS JSON tree and set port profiles on any
    interface entries whose tag query matches an ICS assignment.

    The ``array_key`` tracks which JSON key the current list was stored
    under, so we can look up the correct adapter details group name in
    DICS_INTF_ADAPTER_MAP.
    """
    if isinstance(obj, dict):
        for key, val in obj.items():
            _walk_existing_entries(val, assignments, placed, array_key=key)
    elif isinstance(obj, list):
        adapter_info = DICS_INTF_ADAPTER_MAP.get(array_key)
        for entry in obj:
            if not isinstance(entry, dict):
                continue
            tag_query = (entry.get("tags") or {}).get("query", "")
            if tag_query and tag_query in assignments and adapter_info:
                adapter_key, profile_field = adapter_info
                inputs = entry.setdefault("inputs", {})
                adapter = inputs.setdefault(adapter_key, {})
                adapter[profile_field] = assignments[tag_query]
                placed.add(tag_query)
                debug(f"Updated existing DICS entry: {tag_query}")
            # Continue recursing into nested dicts (skip "tags" to avoid
            # descending into tag metadata).
            for key, val in entry.items():
                if key != "tags":
                    _walk_existing_entries(
                        val, assignments, placed, array_key=key
                    )


# ---------------------------------------------------------------------------
# ICS detachment — remove migrated assignments from the ICS
# ---------------------------------------------------------------------------

def detach_ics_assignments(ics_data, placed):
    """Remove migrated interface assignments from the ICS input data.

    For each interface tag query in ``placed``, removes that interface
    entry from its parent device's interface resolver array.  If a device
    has no remaining interface entries after removal, the device entry
    itself is also removed.

    Returns the number of assignments removed.
    """
    removed = 0
    devices = ics_data.get("devices", [])
    devices_to_keep = []
    for device in devices:
        dev_inputs = device.get("inputs", {})
        interfaces = dev_inputs.get("interface", [])
        kept = []
        for intf in interfaces:
            tag_query = (intf.get("tags") or {}).get("query", "")
            if tag_query in placed:
                removed += 1
                debug(f"Detached {tag_query} from ICS")
            else:
                kept.append(intf)
        # Keep the device if it still has interfaces, or if it never had any.
        if kept:
            dev_inputs["interface"] = kept
            devices_to_keep.append(device)
        elif not interfaces:
            devices_to_keep.append(device)
    ics_data["devices"] = devices_to_keep
    return removed


# ---------------------------------------------------------------------------
# CloudVision API helpers
# ---------------------------------------------------------------------------

async def find_studio_ids(channel):
    """List all studios and return the IDs for the ICS and DICS.

    Studios are discovered by matching their display_name against the
    known constants.  The studio IDs are server-side values (e.g.
    "studio-interface-manager") and are not shipped in the SDK.
    """
    service = StudioSummaryServiceStub(channel)
    ics_id = None
    dics_id = None
    async for item in service.get_all(StudioSummaryStreamRequest()):
        summary = item.value
        name = summary.display_name
        if name == ICS_DISPLAY_NAME:
            ics_id = summary.key.studio_id
        elif name == DICS_DISPLAY_NAME:
            dics_id = summary.key.studio_id
    return ics_id, dics_id


async def read_studio_inputs(channel, studio_id):
    """Read all inputs from a studio's mainline state.

    Studio inputs are JSON blobs stored at paths within a studio.  Both
    the ICS and DICS store all data at the root path ("/"), so the
    returned list typically has a single entry.

    Returns a list of (path_segments, inputs_json) tuples.
    """
    service = InputsServiceStub(channel)
    filter_msg = Inputs(
        key=InputsKey(studio_id=studio_id, workspace_id=MAINLINE_WS_ID)
    )
    request = InputsStreamRequest(partial_eq_filter=[filter_msg])

    entries = []
    async for item in service.get_all(request):
        inp = item.value
        path_segments = list(inp.key.path.values) if inp.key.path else []
        inputs_json = inp.inputs if inp.inputs else None
        if inputs_json is not None:
            entries.append((path_segments, inputs_json))
    return entries


def get_root_json(entries):
    """Extract and parse the root-path JSON from a list of input entries."""
    for path_segments, inputs_json in entries:
        if not path_segments:
            return json.loads(inputs_json)
    return {}


async def read_assigned_tags(channel, studio_id):
    """Read the assigned tags query from a studio's mainline state.

    The assigned tags query tells the studio which devices to consider
    when building configuration (e.g. "datacenter:DC1").
    """
    service = AssignedTagsServiceStub(channel)
    filter_msg = AssignedTags(
        key=StudioKey(studio_id=studio_id, workspace_id=MAINLINE_WS_ID)
    )
    request = AssignedTagsStreamRequest(partial_eq_filter=[filter_msg])

    query = None
    async for item in service.get_all(request):
        query = item.value.query
        break
    return query


async def create_workspace(channel, display_name):
    """Create a new workspace and return its UUID."""
    ws_id = str(uuid.uuid4())
    service = WorkspaceConfigServiceStub(channel)
    config = WorkspaceConfig(
        key=WorkspaceKey(workspace_id=ws_id),
        display_name=display_name,
        description="Automated migration of Interface Configuration Studio "
                    "profiles to Data Center Interface Configuration Studio",
    )
    await service.set(WorkspaceConfigSetRequest(value=config))
    return ws_id


async def _set_with_retry(service, request):
    """Call service.set(), retrying on transient gRPC errors.

    After workspace creation, CVaaS may briefly return UNAVAILABLE or
    NOT_FOUND before the workspace is fully registered across the cluster.
    This helper retries up to WS_RETRY_MAX times with WS_RETRY_INTERVAL
    second delays.
    """
    for attempt in range(WS_RETRY_MAX):
        try:
            return await service.set(request)
        except GRPCError as e:
            if e.status in RETRYABLE_STATUSES and attempt < WS_RETRY_MAX - 1:
                debug(f"Retry {attempt + 1}/{WS_RETRY_MAX} "
                      f"({e.status.name})")
                await asyncio.sleep(WS_RETRY_INTERVAL)
                continue
            raise


async def write_studio_root(channel, studio_id, workspace_id, data):
    """Write a full root input JSON blob to a studio within a workspace."""
    service = InputsConfigServiceStub(channel)
    key = InputsKey(
        studio_id=studio_id,
        workspace_id=workspace_id,
        path=RepeatedString(values=[]),
    )
    config = InputsConfig(key=key, inputs=json.dumps(data))
    await _set_with_retry(service, InputsConfigSetRequest(value=config))


async def write_assigned_tags(channel, studio_id, workspace_id, query):
    """Write the assigned tags query to a studio within a workspace."""
    service = AssignedTagsConfigServiceStub(channel)
    config = AssignedTagsConfig(
        key=StudioKey(studio_id=studio_id, workspace_id=workspace_id),
        query=query,
    )
    await _set_with_retry(service, AssignedTagsConfigSetRequest(value=config))


async def build_workspace(channel, workspace_id):
    """Start a workspace build and poll until it completes.

    Returns True on success, False on failure or cancellation.
    """
    ws_config_svc = WorkspaceConfigServiceStub(channel)
    build_id = str(uuid.uuid4())
    # REQUEST_START_BUILD triggers the build; request_id is a unique
    # correlation key for tracking this specific build.
    config = WorkspaceConfig(
        key=WorkspaceKey(workspace_id=workspace_id),
        request=Request.START_BUILD,
        request_params=RequestParams(request_id=build_id),
    )
    await ws_config_svc.set(WorkspaceConfigSetRequest(value=config))

    # Poll the build state until it reaches a terminal state.
    build_svc = WorkspaceBuildServiceStub(channel)
    build_key = WorkspaceBuildKey(workspace_id=workspace_id, build_id=build_id)

    while True:
        await asyncio.sleep(BUILD_POLL_INTERVAL)
        try:
            resp = await build_svc.get_one(
                WorkspaceBuildRequest(key=build_key)
            )
        except GRPCError as e:
            # Build state may not be available immediately.
            if e.status == GRPCStatus.UNAVAILABLE:
                continue
            raise
        state = resp.value.state
        if state == BuildState.SUCCESS:
            return True
        if state in (BuildState.FAIL, BuildState.CANCELED):
            error = resp.value.error or "unknown error"
            print(f"Error: workspace build failed: {error}", file=sys.stderr)
            return False


async def submit_workspace(channel, workspace_id):
    """Submit a workspace and return the resulting change control IDs.

    Submitting a workspace commits its changes and automatically creates
    one or more change controls.  The CC IDs are read from the workspace
    state once it transitions to SUBMITTED.
    """
    ws_config_svc = WorkspaceConfigServiceStub(channel)
    submit_id = str(uuid.uuid4())
    config = WorkspaceConfig(
        key=WorkspaceKey(workspace_id=workspace_id),
        request=Request.SUBMIT,
        request_params=RequestParams(request_id=submit_id),
    )
    await ws_config_svc.set(WorkspaceConfigSetRequest(value=config))

    # Poll workspace state until submission completes.
    ws_svc = WorkspaceServiceStub(channel)
    while True:
        await asyncio.sleep(BUILD_POLL_INTERVAL)
        try:
            resp = await ws_svc.get_one(
                WorkspaceRequest(key=WorkspaceKey(workspace_id=workspace_id))
            )
        except GRPCError as e:
            if e.status == GRPCStatus.UNAVAILABLE:
                continue
            raise
        ws = resp.value
        if ws.state == WorkspaceState.SUBMITTED:
            cc_ids = list(ws.cc_ids.values) if ws.cc_ids else []
            return cc_ids
        if ws.state in (WorkspaceState.CONFLICTS, WorkspaceState.ABANDONED,
                        WorkspaceState.ROLLED_BACK):
            print(f"Error: workspace submission failed "
                  f"(state: {ws.state.name})", file=sys.stderr)
            return []


async def approve_and_execute_cc(channel, cc_id):
    """Approve a change control and start its execution."""
    # Read the current CC version (required for the approve call).
    cc_svc = ChangeControlServiceStub(channel)
    resp = await cc_svc.get_one(
        ChangeControlRequest(key=ChangeControlKey(id=cc_id))
    )
    cc_version = resp.value.change.time if resp.value.change else None

    # Approve the change control.
    approve_svc = ApproveConfigServiceStub(channel)
    approve = ApproveConfig(
        key=ChangeControlKey(id=cc_id),
        approve=FlagConfig(value=True),
    )
    if cc_version:
        approve.version = cc_version
    await approve_svc.set(ApproveConfigSetRequest(value=approve))
    print(f"  Change control {cc_id} approved")

    # Start execution.
    cc_config_svc = ChangeControlConfigServiceStub(channel)
    start_config = ChangeControlConfig(
        key=ChangeControlKey(id=cc_id),
        start=FlagConfig(value=True),
    )
    await cc_config_svc.set(ChangeControlConfigSetRequest(value=start_config))
    print(f"  Change control {cc_id} execution started")


async def abandon_workspace(channel, workspace_id):
    """Abandon a workspace to clean up after a failure."""
    ws_config_svc = WorkspaceConfigServiceStub(channel)
    config = WorkspaceConfig(
        key=WorkspaceKey(workspace_id=workspace_id),
        request=Request.ABANDON,
        request_params=RequestParams(request_id=str(uuid.uuid4())),
    )
    try:
        await ws_config_svc.set(WorkspaceConfigSetRequest(value=config))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Discover mode — print studio schemas and input data for debugging
# ---------------------------------------------------------------------------

def print_schema_field(field_id, field, indent=0):
    """Print a single studio input field with its type, label, and metadata.

    Shows the field's ``name`` attribute (the actual JSON key) when it
    differs from the schema field ID.  For STRING fields with fixed
    options, prints the allowed values.
    """
    prefix = "  " * indent
    type_name = FIELD_TYPE_NAMES.get(field.type, str(field.type))
    name_info = ""
    if field.name and field.name != field_id:
        name_info = f" [name={field.name}]"
    print(f"{prefix}- {field_id} ({type_name}): "
          f"{field.label or ''}{name_info}")
    if field.resolver_props and field.resolver_props.input_tag_label:
        print(f"{prefix}    tag_label: {field.resolver_props.input_tag_label}")
        print(f"{prefix}    input_mode: {field.resolver_props.input_mode}")
    if field.group_props and field.group_props.members:
        print(f"{prefix}    members: {field.group_props.members.values}")
    if field.collection_props and field.collection_props.base_field_id:
        print(f"{prefix}    base_field: "
              f"{field.collection_props.base_field_id}")
    if field.string_props and field.string_props.static_options:
        opts = field.string_props.static_options.values
        if opts:
            print(f"{prefix}    options: {opts}")


async def read_studio_schema(channel, studio_id):
    """Read and return the input schema for a studio."""
    service = StudioServiceStub(channel)
    resp = await service.get_one(
        StudioRequest(
            key=StudioKey(studio_id=studio_id, workspace_id=MAINLINE_WS_ID)
        )
    )
    return resp.value.input_schema


async def discover_schemas(channel, ics_id, dics_id):
    """Print the input schemas and current inputs for both studios.

    This is a read-only operation that makes no changes.  Useful for
    understanding the field structure and valid option values before
    running a migration.
    """
    print("\n=== Interface Configuration Studio Schema ===")
    ics_schema = await read_studio_schema(channel, ics_id)
    if ics_schema and ics_schema.fields and ics_schema.fields.values:
        for field_id, field in ics_schema.fields.values.items():
            print_schema_field(field_id, field)
    else:
        print("  (no schema fields found)")

    print("\n=== Data Center Interface Configuration Studio Schema ===")
    dics_schema = await read_studio_schema(channel, dics_id)
    if dics_schema and dics_schema.fields and dics_schema.fields.values:
        for field_id, field in dics_schema.fields.values.items():
            print_schema_field(field_id, field)
    else:
        print("  (no schema fields found)")

    print("\n=== ICS Input Paths ===")
    entries = await read_studio_inputs(channel, ics_id)
    for path_segments, inputs_json in entries:
        path_str = "/" + "/".join(path_segments) if path_segments else "/"
        preview = inputs_json[:200] if inputs_json else "(empty)"
        print(f"  {path_str}: {preview}")

    print("\n=== DICS Input Paths (existing) ===")
    dics_entries = await read_studio_inputs(channel, dics_id)
    if dics_entries:
        for path_segments, inputs_json in dics_entries:
            path_str = "/" + "/".join(path_segments) if path_segments else "/"
            preview = inputs_json[:200] if inputs_json else "(empty)"
            print(f"  {path_str}: {preview}")
    else:
        print("  (no existing inputs)")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(
        description="Migrate profiles from Interface Configuration Studio "
                    "to Data Center Interface Configuration Studio in CVaaS"
    )
    parser.add_argument(
        "--server", required=True,
        help="CVaaS server address (host:port)",
    )
    parser.add_argument(
        "--token-file", required=True,
        help="Path to service account token file",
    )
    parser.add_argument(
        "--mode",
        choices=["discover", "workspace-only", "submit-workspace",
                 "submit-all"],
        default="workspace-only",
        help="discover: print both studio schemas and input paths, then "
             "exit (no changes made). workspace-only: build workspace and "
             "leave open for review (default). submit-workspace: submit "
             "workspace, leave change control open. submit-all: submit "
             "workspace and approve/execute the change control.",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="Skip TLS certificate verification",
    )
    parser.add_argument(
        "--workspace-name",
        default="ICS to DICS Profile Migration",
        help="Display name for the workspace "
             "(default: 'ICS to DICS Profile Migration')",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable verbose debug output",
    )
    args = parser.parse_args()

    # Set the module-level debug flag so all functions can use it.
    global DEBUG
    DEBUG = args.debug

    with open(args.token_file) as f:
        token = f.read().strip()

    # Split "host:port" — the SDK requires them as separate arguments.
    host, _, port_str = args.server.partition(":")
    port = int(port_str) if port_str else 443

    client = AsyncCVClient.from_token(token, host, port=port,
                                      insecure=args.insecure)
    with client as channel:
        # --- Discover studio IDs by display name ---
        print("Discovering studios...")
        ics_id, dics_id = await find_studio_ids(channel)
        if not ics_id:
            print(f"Error: could not find studio "
                  f"'{ICS_DISPLAY_NAME}'", file=sys.stderr)
            sys.exit(1)
        if not dics_id:
            print(f"Error: could not find studio "
                  f"'{DICS_DISPLAY_NAME}'", file=sys.stderr)
            sys.exit(1)
        print(f"  ICS: {ics_id}")
        print(f"  DICS: {dics_id}")

        # In discover mode, print schemas and exit without making changes.
        if args.mode == "discover":
            await discover_schemas(channel, ics_id, dics_id)
            return

        # --- Read ICS data ---
        print("Reading ICS data...")
        ics_entries = await read_studio_inputs(channel, ics_id)
        if not ics_entries:
            print("No data found in Interface Configuration Studio.",
                  file=sys.stderr)
            sys.exit(1)

        ics_data = get_root_json(ics_entries)
        profiles, assignments = parse_ics_data(json.dumps(ics_data))
        print(f"  Profiles: {len(profiles)}")
        for name in profiles:
            print(f"    - {name}")
        print(f"  Interface assignments: {len(assignments)}")
        for tag, prof in assignments.items():
            print(f"    {tag} -> {prof}")

        if not profiles and not assignments:
            print("No profiles or assignments found to migrate.",
                  file=sys.stderr)
            sys.exit(1)

        # --- Read existing DICS data ---
        print("Reading existing DICS data...")
        dics_entries = await read_studio_inputs(channel, dics_id)
        dics_data = get_root_json(dics_entries) if dics_entries else {}

        # --- Build DICS port profiles from ICS profiles ---
        new_port_profiles = build_dics_port_profiles(profiles)
        existing_profiles = dics_data.get("portProfiles", [])
        existing_names = set()
        for ep in existing_profiles:
            n = ep.get("name")
            if n:
                existing_names.add(n)

        added = 0
        for pp in new_port_profiles:
            name = pp.get("name")
            if name and name not in existing_names:
                existing_profiles.append(pp)
                existing_names.add(name)
                added += 1
            elif name and name in existing_names:
                print(f"  Skipping profile '{name}' (already exists in DICS)")
        dics_data["portProfiles"] = existing_profiles
        print(f"  Added {added} port profile(s) to DICS")

        # Debug: show the portfast mapping for each profile.
        for pp_name, ics_prof in profiles.items():
            pf = ics_prof.get("portFastEnabled")
            mapped = next((p for p in new_port_profiles
                           if p.get("name") == pp_name), {})
            st = mapped.get("spanningTree", {})
            pc = mapped.get("portChannel", {})
            debug(f"{pp_name}: portFastEnabled={pf!r} -> spanningTree={st}")
            if pc:
                debug(f"  portChannel={pc}")
            # Show raw ICS port channel fields for debugging
            for k in ("channelGroup", "mlagEnabled", "lacpEnabled",
                      "profileLACPConfiguration"):
                v = ics_prof.get(k)
                if v is not None:
                    debug(f"  ICS {k}={v!r}")

        # --- Apply interface assignments ---
        placed = set()
        if assignments:
            # Extract device names from the interface tag queries so we
            # can look up their DC/Pod/Domain tags.
            device_names = set()
            for intf_query in assignments:
                tag_val = (intf_query.split(":", 1)[1]
                           if ":" in intf_query else "")
                dev = tag_val.split("@", 1)[1] if "@" in tag_val else ""
                if dev:
                    device_names.add(dev)

            print("Reading device tags for hierarchy placement...")
            device_hier = await read_device_tags(channel, device_names)
            for dev in sorted(device_names):
                h = device_hier.get(dev, {})
                print(f"  {dev}: DC={h.get('DC')}, "
                      f"DC-Pod={h.get('DC-Pod')}, "
                      f"Leaf-Domain={h.get('Leaf-Domain')}")

            placed = apply_assignments_to_dics(
                dics_data, assignments, device_hier,
            )
            print(f"  Placed {len(placed)}/{len(assignments)} interface "
                  f"assignment(s) in DICS hierarchy")

        # --- Detach migrated assignments from ICS ---
        if assignments and placed:
            detached = detach_ics_assignments(ics_data, placed)
            print(f"  Detaching {detached} migrated assignment(s) from ICS")

        # --- Read ICS assigned tags query ---
        tags_query = await read_assigned_tags(channel, ics_id)

        # --- Create workspace and write changes ---
        ws_id = await create_workspace(channel, args.workspace_name)
        print(f"Created workspace: {ws_id}")

        # Write the modified DICS data (new profiles + interface assignments).
        print("Writing DICS data to workspace...")
        await write_studio_root(channel, dics_id, ws_id, dics_data)
        print("  Wrote DICS root input")

        if tags_query:
            await write_assigned_tags(channel, dics_id, ws_id, tags_query)
            print(f"  Wrote DICS assigned tags query: {tags_query}")

        # Write the modified ICS data (detached interface assignments).
        if assignments and placed:
            print("Writing ICS data to workspace (detaching profiles)...")
            await write_studio_root(channel, ics_id, ws_id, ics_data)
            print("  Wrote ICS root input")

        # --- Build the workspace ---
        print("Building workspace...")
        build_ok = await build_workspace(channel, ws_id)
        if not build_ok:
            print("Abandoning workspace due to build failure...",
                  file=sys.stderr)
            await abandon_workspace(channel, ws_id)
            sys.exit(1)
        print("  Build succeeded")

        if args.mode == "workspace-only":
            print(f"\nWorkspace '{ws_id}' is ready for review.")
            print("Open CloudVision to inspect the workspace before "
                  "submitting.")
            return

        # --- Submit the workspace ---
        print("Submitting workspace...")
        cc_ids = await submit_workspace(channel, ws_id)
        if not cc_ids:
            print("Error: workspace submitted but no change controls "
                  "were created", file=sys.stderr)
            sys.exit(1)
        print(f"  Workspace submitted. Change control(s): "
              f"{', '.join(cc_ids)}")

        if args.mode == "submit-workspace":
            print(f"\nChange control(s) created and ready for review:")
            for cc_id in cc_ids:
                print(f"  {cc_id}")
            return

        # --- Approve and execute change control(s) ---
        print("Approving and executing change control(s)...")
        for cc_id in cc_ids:
            await approve_and_execute_cc(channel, cc_id)

        print("\nMigration complete. Change control(s) have been executed.")


if __name__ == "__main__":
    asyncio.run(main())
