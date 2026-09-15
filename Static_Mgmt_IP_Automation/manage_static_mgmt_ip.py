#!/usr/bin/env python3
"""Populate Management Connectivity Studio from a CSV file.

The script creates a CloudVision workspace, replaces the Device Addressing
collection with the rows in the CSV, and then performs the workspace action
selected with ``--mode``.

Use ``--mode discover`` first when working with a new CloudVision version.  It
prints the Management Connectivity Studio schema and its current root JSON,
which makes it easy to confirm the server's exact field names.
"""

import argparse
import asyncio
import csv
import ipaddress
import json
import logging
import sys
import uuid
from pathlib import Path

from grpclib.const import Status as GRPCStatus
from grpclib.exceptions import GRPCError

from cloudvision.api.client import AsyncCVClient
from cloudvision.api.fmp import RepeatedString
from cloudvision.api.arista.inventory.v1 import (
    DeviceServiceStub,
    DeviceStreamRequest,
)
from cloudvision.api.arista.studio.v1 import (
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
from cloudvision.api.arista.workspace.v1 import (
    BuildState,
    Request,
    RequestParams,
    WorkspaceBuildKey,
    WorkspaceBuildRequest,
    WorkspaceBuildServiceStub,
    WorkspaceConfig,
    WorkspaceConfigServiceStub,
    WorkspaceConfigSetRequest,
    WorkspaceKey,
    WorkspaceRequest,
    WorkspaceServiceStub,
    WorkspaceState,
)
# The imports below are grouped by the CloudVision service used by the script:
# inventory finds serial numbers, studio handles schema/input data, workspace
# controls the workspace lifecycle, and changecontrol approves the result.
from cloudvision.api.arista.changecontrol.v1 import (
    ApproveConfig,
    ApproveConfigServiceStub,
    ApproveConfigSetRequest,
    ChangeControlKey,
    ChangeControlRequest,
    ChangeControlServiceStub,
    FlagConfig,
)


# CloudVision uses an empty workspace ID to mean the committed mainline.
# Every read before the workspace is created therefore uses this value.
# The constants below keep the server-specific names in one easy-to-find place.
MAINLINE_WORKSPACE = ""
STUDIO_NAME = "Management Connectivity"
ROOT_COLLECTION = "deviceAddressing"
CSV_COLUMNS = (
    "device",
    "mgmtVRF",
    "enabled",
    "ipv4Address",
    "defaultGateway",
    "additionalInterfaces",
)
POLL_SECONDS = 5
RETRYABLE_STATUSES = {GRPCStatus.UNAVAILABLE, GRPCStatus.NOT_FOUND}
LOG = logging.getLogger("static-mgmt-ip")


# ----- Command-line and CSV input helpers -----

def parse_server(value):
    """Return the hostname and port expected by AsyncCVClient.

    The accepted forms are host, host:port, IPv4, IPv4:port, and bracketed
    IPv6 with an optional port.  CloudVision normally uses port 443.
    """
    if value.startswith("[") and "]" in value:
        host, _, suffix = value[1:].partition("]")
        port = int(suffix[1:]) if suffix.startswith(":") else 443
        return host, port
    if value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
        if port_text.isdigit():
            return host, int(port_text)
    return value, 443


def parse_bool(value, column, row_number):
    """Convert common CSV boolean spellings into a Python boolean."""
    # Normalize capitalization and whitespace before comparing the user value.
    normalized = value.strip().lower()
    if normalized in {"true", "t", "yes", "y", "1", "enabled"}:
        return True
    if normalized in {"false", "f", "no", "n", "0", "disabled"}:
        return False
    raise ValueError(
        f"row {row_number}: {column} must be true/false (got {value!r})"
    )


def parse_additional_interfaces(value, row_number):
    """Convert the CSV list into JSON-compatible values.

    A JSON list is accepted for complex interface values.  For convenience,
    a plain comma-separated list such as ``Ethernet1,Ethernet2`` is accepted
    too.  An empty CSV cell becomes an empty list.
    """
    # An empty cell means that no additional interface was requested.
    value = value.strip()
    if not value:
        return []
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"row {row_number}: additionalInterfaces is not valid JSON"
            ) from error
    return [item.strip() for item in value.split(",") if item.strip()]


def validate_ipv4_fields(ipv4_address, default_gateway, row_number):
    """Validate the required device address and default gateway values."""
    # A management address must include a prefix length, not just a host address.
    if "/" not in ipv4_address:
        raise ValueError(f"row {row_number}: ipv4Address must use CIDR notation, for example 192.0.2.10/24")
    # ip_interface validates both the IPv4 address and its CIDR prefix.
    try:
        address = ipaddress.ip_interface(ipv4_address)
    except ValueError as error:
        raise ValueError(f"row {row_number}: ipv4Address is not a valid IPv4 CIDR address: {ipv4_address!r}") from error
    if address.version != 4:
        raise ValueError(f"row {row_number}: ipv4Address must be IPv4: {ipv4_address!r}")
    if not default_gateway:
        raise ValueError(f"row {row_number}: defaultGateway is required")
    try:
        gateway = ipaddress.ip_address(default_gateway)
    except ValueError as error:
        raise ValueError(f"row {row_number}: defaultGateway is not a valid IPv4 address: {default_gateway!r}") from error
    if gateway.version != 4 or "/" in default_gateway:
        raise ValueError(f"row {row_number}: defaultGateway must be a plain IPv4 address without CIDR notation: {default_gateway!r}")


# ----- CSV-to-studio data conversion -----

def read_csv(path):
    """Read and validate the user CSV, returning studio-ready dictionaries."""
    rows = []
    with Path(path).open(newline="", encoding="utf-8-sig") as csv_file:
        # DictReader lets the remaining code refer to columns by their names.
        reader = csv.DictReader(csv_file)
        actual = tuple(reader.fieldnames or ())
        missing = [column for column in CSV_COLUMNS if column not in actual]
        if missing:
            raise ValueError("CSV is missing required columns: " + ", ".join(missing))
        # Start at line 2 because line 1 contains the CSV header.
        for row_number, raw in enumerate(reader, start=2):
            if not any((value or "").strip() for value in raw.values()):
                continue
            ipv4_address = raw["ipv4Address"].strip()
            default_gateway = raw["defaultGateway"].strip()
            if not ipv4_address:
                raise ValueError(f"row {row_number}: ipv4Address is required")
            # Validate before appending so no invalid row reaches CloudVision.
            validate_ipv4_fields(ipv4_address, default_gateway, row_number)
            rows.append({
                "device": raw["device"].strip(),
                "mgmtVRF": raw["mgmtVRF"].strip(),
                "enabled": parse_bool(raw["enabled"], "enabled", row_number),
                "ipv4Address": ipv4_address,
                "defaultGateway": default_gateway,
                "additionalInterfaces": parse_additional_interfaces(
                    raw["additionalInterfaces"], row_number
                ),
            })
            LOG.debug("CSV row %d: %s", row_number, rows[-1])
    if not rows:
        raise ValueError("CSV does not contain any device rows")
    return rows


# ----- CloudVision inventory and studio mapping helpers -----

async def build_device_serial_map(channel):
    """Return a case-insensitive hostname-to-serial-number inventory map."""
    # Inventory records use device_id for the serial number and hostname for the name.
    service = DeviceServiceStub(channel)
    device_map = {}
    async for item in service.get_all(DeviceStreamRequest()):
        device = item.value
        serial = device.key.device_id if device.key else ""
        hostname = (device.hostname or "").strip()
        if serial and hostname:
            device_map[hostname.lower()] = serial
    return device_map


def build_device_addressing(rows, serial_by_hostname):
    """Convert CSV rows into tagged Management Connectivity resolver entries."""
    # Each entry becomes one resolver rule in the Device Addressing collection.
    entries = []
    unresolved = []
    for row in rows:
        # The CSV contains a hostname, but the studio query requires a serial.
        device_value = row["device"].strip()
        serial = serial_by_hostname.get(device_value.lower(), "")
        if not serial:
            unresolved.append(device_value)
            continue
        # These keys mirror the nested group names reported by --mode discover.
        inputs = {
            "managementInterface": {
                "ipConfiguration": {
                    "ipv4Configuration": {
                        "ipv4Address": {"ipv4Address": row["ipv4Address"]}
                    }
                },
                "enabled": "Yes" if row["enabled"] else "No",
            },
            "managementVrf": row["mgmtVRF"],
            "defaultGateway": {
                "ipv4Gateways": ([{"ipv4Address": row["defaultGateway"]}]
                                  if row["defaultGateway"] else [])
            },
            "otherInterfaces": build_other_interfaces(row["additionalInterfaces"]),
        }
        # The resolver uses the group name as the input envelope.
        entries.append({"inputs": {"deviceAddressing": inputs},
                        "tags": {"query": f"device:{serial}"}})
    if unresolved:
        raise ValueError("Could not resolve device hostname(s): " + ", ".join(unresolved))
    return entries


def build_other_interfaces(values):
    """Convert interface names or JSON interface objects into schema entries."""
    interfaces = []
    for value in values:
        if isinstance(value, dict):
            name = value.get("interfaceName", "")
            address = value.get("ipv4Address", "")
        else:
            name, address = str(value), ""
        interface = {"interfaceName": name}
        if address:
            interface["interfaceDetails"] = {
                "ipConfiguration": {
                    "ipv4Configuration": {
                        "ipv4Address": {"ipv4Address": address}
                    }
                }
            }
        interfaces.append(interface)
    return interfaces


# ----- CloudVision API lifecycle helpers -----

async def find_studio(channel):
    """Find the studio ID by its display name."""
    service = StudioSummaryServiceStub(channel)
    async for item in service.get_all(StudioSummaryStreamRequest()):
        summary = item.value
        display_name = (summary.display_name or "").strip().lower()
        if display_name in {"management connectivity", "management connectivity studio"}:
            return summary.key.studio_id
    return None


async def read_inputs(channel, studio_id, workspace_id=MAINLINE_WORKSPACE):
    """Read all JSON input blobs for a studio and return the root JSON."""
    # Ask only for this studio and workspace so unrelated inputs are ignored.
    service = InputsServiceStub(channel)
    filter_message = Inputs(
        key=InputsKey(studio_id=studio_id, workspace_id=workspace_id)
    )
    request = InputsStreamRequest(partial_eq_filter=[filter_message])
    async for item in service.get_all(request):
        input_value = item.value
        path = list(input_value.key.path.values) if input_value.key.path else []
        if not path and input_value.inputs:
            return json.loads(input_value.inputs)
    return {}


def schema_type_name(field_type):
    """Make enum values readable across cloudvision package versions."""
    return getattr(field_type, "name", str(field_type))


async def discover(channel, studio_id):
    """Print the schema and current root data without changing CloudVision."""
    studio = await StudioServiceStub(channel).get_one(
        StudioRequest(key=StudioKey(studio_id=studio_id, workspace_id=MAINLINE_WORKSPACE))
    )
    schema = studio.value.input_schema
    print(f"Studio: {STUDIO_NAME}\nStudio ID: {studio_id}\n\nSchema:")
    fields = schema.fields.values if schema and schema.fields else {}
    for field_id, field in fields.items():
        print(f"- {field_id}: {schema_type_name(field.type)}; name={field.name!r}; label={field.label!r}")
        if field.group_props and field.group_props.members:
            print(f"  members: {list(field.group_props.members.values)}")
        if field.collection_props and field.collection_props.base_field_id:
            print(f"  collection base field: {field.collection_props.base_field_id}")
        if field.string_props and field.string_props.static_options:
            print(f"  options: {list(field.string_props.static_options.values)}")
    print("\nCurrent root input:")
    print(json.dumps(await read_inputs(channel, studio_id), indent=2, sort_keys=True))


async def create_workspace(channel, name):
    """Create an empty workspace and return its server-side UUID."""
    # UUIDs make the workspace ID unique even when the script is run repeatedly.
    workspace_id = str(uuid.uuid4())
    config = WorkspaceConfig(
        key=WorkspaceKey(workspace_id=workspace_id),
        display_name=name,
        description="Populate Management Connectivity Device Addressing from CSV",
    )
    await WorkspaceConfigServiceStub(channel).set(
        WorkspaceConfigSetRequest(value=config)
    )
    return workspace_id


async def set_with_retry(service, request):
    """Retry transient writes while a newly-created workspace propagates."""
    for attempt in range(30):
        try:
            return await service.set(request)
        except GRPCError as error:
            if error.status not in RETRYABLE_STATUSES or attempt == 29:
                raise
            LOG.debug("Retrying workspace write after %s", error.status.name)
            await asyncio.sleep(2)


async def write_root(channel, studio_id, workspace_id, data):
    """Write the complete modified root JSON into the workspace."""
    # An empty path means that the complete studio root is being replaced.
    key = InputsKey(
        studio_id=studio_id,
        workspace_id=workspace_id,
        path=RepeatedString(values=[]),
    )
    config = InputsConfig(key=key, inputs=json.dumps(data))
    await set_with_retry(
        InputsConfigServiceStub(channel), InputsConfigSetRequest(value=config)
    )


async def build_workspace(channel, workspace_id):
    """Build the workspace and return whether CloudVision accepted the build."""
    # A separate build ID lets us poll the exact build started below.
    build_id = str(uuid.uuid4())
    config = WorkspaceConfig(
        key=WorkspaceKey(workspace_id=workspace_id),
        request=Request.START_BUILD,
        request_params=RequestParams(request_id=build_id),
    )
    await WorkspaceConfigServiceStub(channel).set(
        WorkspaceConfigSetRequest(value=config)
    )
    service = WorkspaceBuildServiceStub(channel)
    while True:
        # Builds are asynchronous, so wait briefly between status checks.
        await asyncio.sleep(POLL_SECONDS)
        try:
            response = await service.get_one(
                WorkspaceBuildRequest(
                    key=WorkspaceBuildKey(workspace_id=workspace_id, build_id=build_id)
                )
            )
        except GRPCError as error:
            if error.status == GRPCStatus.UNAVAILABLE:
                continue
            raise
        result = response.value
        if result.state == BuildState.SUCCESS:
            return True
        if result.state in (BuildState.FAIL, BuildState.CANCELED):
            print(f"Workspace build failed: {result.error or 'unknown error'}", file=sys.stderr)
            details = result.studio_build_details
            validation = getattr(details, "input_validation_results", None)
            for studio_id, item in (getattr(validation, "values", {}) or {}).items():
                for errors_name in ("input_schema_errors", "input_value_errors"):
                    errors = getattr(item, errors_name, None)
                    for error in (getattr(errors, "values", []) or []):
                        path = "/".join(getattr(error.path, "values", []) or [])
                        members = ", ".join(getattr(error.members, "values", []) or [])
                        print(f"  {studio_id} {errors_name}: field={error.field_id!r} path={path!r} members={members!r} message={error.message!r}", file=sys.stderr)
            return False


async def submit_workspace(channel, workspace_id):
    """Submit a built workspace and return its generated change-control IDs."""
    config = WorkspaceConfig(
        key=WorkspaceKey(workspace_id=workspace_id),
        request=Request.SUBMIT,
        request_params=RequestParams(request_id=str(uuid.uuid4())),
    )
    await WorkspaceConfigServiceStub(channel).set(
        WorkspaceConfigSetRequest(value=config)
    )
    service = WorkspaceServiceStub(channel)
    while True:
        await asyncio.sleep(POLL_SECONDS)
        response = await service.get_one(
            WorkspaceRequest(key=WorkspaceKey(workspace_id=workspace_id))
        )
        workspace = response.value
        if workspace.state == WorkspaceState.SUBMITTED:
            return list(workspace.cc_ids.values) if workspace.cc_ids else []
        if workspace.state in (WorkspaceState.CONFLICTS, WorkspaceState.ABANDONED,
                               WorkspaceState.ROLLED_BACK):
            raise RuntimeError(f"Workspace submission failed: {workspace.state.name}")


async def approve_change_control(channel, change_control_id):
    """Approve one change control, leaving execution to normal CV workflow."""
    response = await ChangeControlServiceStub(channel).get_one(
        ChangeControlRequest(key=ChangeControlKey(id=change_control_id))
    )
    approval = ApproveConfig(
        key=ChangeControlKey(id=change_control_id),
        approve=FlagConfig(value=True),
    )
    if response.value.change and response.value.change.time:
        approval.version = response.value.change.time
    await ApproveConfigServiceStub(channel).set(
        ApproveConfigSetRequest(value=approval)
    )


# ----- Main workflow and command-line interface -----

async def run(args):
    """Run the complete read, transform, workspace, and mode workflow."""
    # Read the token locally; it is never written into the workspace data.
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("token file is empty")
    host, port = parse_server(args.server)
    client = AsyncCVClient.from_token(token, host, port=port, insecure=args.insecure)
    with client as channel:
        studio_id = await find_studio(channel)
        if not studio_id:
            raise RuntimeError(f"Could not find studio named {STUDIO_NAME!r}")
        print(f"Using {STUDIO_NAME} studio: {studio_id}")
        if args.mode == "discover":
            await discover(channel, studio_id)
            return

        print(f"Reading and validating CSV file: {args.input_file}")
        rows = read_csv(args.input_file)
        print(f"CSV validation succeeded: {len(rows)} row(s) read")
        print("Looking up device serial numbers in CloudVision inventory...")
        serial_by_hostname = await build_device_serial_map(channel)
        # Preserve management profiles, assignments, and all other studio data.
        root = await read_inputs(channel, studio_id)
        root[ROOT_COLLECTION] = build_device_addressing(rows, serial_by_hostname)
        # Create the workspace only after all local validation has succeeded.
        workspace_id = await create_workspace(channel, args.workspace_name)
        print(f"Created workspace: {workspace_id}")
        await write_root(channel, studio_id, workspace_id, root)
        print(f"Wrote {len(rows)} Device Addressing row(s)")
        if not await build_workspace(channel, workspace_id):
            raise RuntimeError("Workspace build failed")
        print("Workspace build succeeded")
        # workspace-only stops here; the user can review it in CloudVision.
        if args.mode == "workspace-only":
            print("Workspace left open for review.")
            return

        # Other modes submit the workspace and receive one or more change controls.
        change_controls = await submit_workspace(channel, workspace_id)
        if not change_controls:
            raise RuntimeError("Workspace submitted without a change control")
        print("Submitted workspace; change control(s): " + ", ".join(change_controls))
        if args.mode == "submit-workspace":
            print("Change control(s) left open for approval.")
            return
        # submit-all approves every change control created by the submission.
        for change_control_id in change_controls:
            await approve_change_control(channel, change_control_id)
            print(f"Approved change control: {change_control_id}")


def build_parser():
    """Define command-line arguments and their user-facing help text."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="CloudVision host or host:port")
    parser.add_argument("--token-file", required=True, help="File containing the auth token")
    parser.add_argument("--input-file", help="CSV containing Device Addressing rows")
    parser.add_argument("--mode", required=True,
                        choices=("discover", "workspace-only", "submit-workspace", "submit-all"),
                        help="discover: print schema and current data; workspace-only: build and leave open; submit-workspace: submit and leave change control open; submit-all: submit and approve")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate checking")
    parser.add_argument("--debug", action="store_true", help="Enable debug-level messages")
    parser.add_argument("--workspace-name", default="Static Management IP Automation",
                        help="Display name for the new workspace")
    return parser


def main():
    """Parse arguments, validate normal-mode requirements, and start asyncio."""
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s: %(message)s")
    LOG.setLevel(logging.DEBUG if args.debug else logging.INFO)
    for logger_name in ("cloudvision", "arista", "grpclib", "grpc"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    if args.mode != "discover" and not args.input_file:
        parser.error("--input-file is required unless --mode discover is used")
    try:
        asyncio.run(run(args))
    except (OSError, ValueError, RuntimeError, GRPCError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
