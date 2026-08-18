#!/usr/bin/env python3
"""Look up MAC address locations in Arista CloudVision.

Reads a list of MAC addresses from a file and queries CloudVision's
Endpoint Location API to find the switch and port where each MAC resides.
"""

import argparse
import asyncio
import csv
import re
import sys

from cloudvision.api.client import AsyncCVClient
from cloudvision.api.arista.endpointlocation.v1 import (
    EndpointLocationServiceStub,
    EndpointLocationKey,
    EndpointLocationSomeRequest,
)
from cloudvision.api.arista.inventory.v1 import (
    DeviceServiceStub,
    DeviceStreamRequest,
)


# Regex pattern to validate MAC address formats:
#   xx:xx:xx:xx:xx:xx  (colon-separated)
#   xx-xx-xx-xx-xx-xx  (dash-separated)
#   xxxx.xxxx.xxxx     (Cisco dot notation)
MAC_PATTERN = re.compile(
    r"^([0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}$"
    r"|^([0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4}$"
)


def normalize_mac(mac: str) -> str:
    """Convert any MAC format to lowercase colon-separated (e.g. aa:bb:cc:dd:ee:ff)."""
    # Strip all delimiters to get a raw 12-character hex string
    raw = mac.replace(":", "").replace("-", "").replace(".", "").lower()
    # Re-insert colons every 2 characters
    return ":".join(raw[i:i+2] for i in range(0, 12, 2))


def read_mac_file(path: str) -> list[str]:
    """Read MAC addresses from a file, one per line. Skips blank lines and comments (#)."""
    macs = []
    with open(path) as f:
        # enumerate starts at 1 so line_num matches the actual line number in the file
        for line_num, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not MAC_PATTERN.match(stripped):
                print(f"Warning: skipping invalid MAC on line {line_num}: {stripped}",
                      file=sys.stderr)
                continue
            macs.append(stripped)
    return macs


async def build_hostname_map(channel) -> dict[str, str]:
    """Query CloudVision inventory to build a mapping of device serial number -> hostname.

    This lets us display friendly hostnames instead of serial numbers in the output.
    """
    service = DeviceServiceStub(channel)
    hostnames = {}
    # get_all streams every device in the inventory; we iterate with 'async for'
    # because the CloudVision SDK uses asynchronous gRPC streaming
    async for item in service.get_all(DeviceStreamRequest()):
        device = item.value
        if device.key.device_id and device.hostname:
            hostnames[device.key.device_id] = device.hostname
    return hostnames


async def lookup_macs(channel, macs: list[str]) -> list[dict]:
    """Query CloudVision's Endpoint Location API for a list of MAC addresses.

    Returns a list of dicts, each containing the search_term and its locations
    (device_id, interface, vlan) or an error.
    """
    service = EndpointLocationServiceStub(channel)
    # Build a batch request with all MACs so we make a single API call
    keys = [EndpointLocationKey(search_term=mac) for mac in macs]
    request = EndpointLocationSomeRequest(keys=keys)

    results = []
    # get_some returns a streamed response — one item per requested MAC
    async for resp in service.get_some(request):
        if resp.error:
            results.append({"search_term": None, "error": resp.error})
            continue

        endpoint = resp.value
        search_term = endpoint.key.search_term

        # If CloudVision has no location data for this MAC, record it as empty
        if not endpoint.device_map or not endpoint.device_map.values:
            results.append({"search_term": search_term, "locations": []})
            continue

        # device_map.values is a dict keyed by device serial/MAC;
        # each value is a Device with a location_list of possible locations
        locations = []
        for _dev_key, device in endpoint.device_map.values.items():
            if device.location_list and device.location_list.values:
                for loc in device.location_list.values:
                    locations.append({
                        "device_id": loc.device_id,
                        "interface": loc.interface,
                        "vlan": loc.vlan_id,
                    })
        results.append({"search_term": search_term, "locations": locations})

    return results


def format_table(rows: list[list[str]], headers: list[str]) -> str:
    """Format rows and headers into an ASCII table with auto-sized columns."""
    all_rows = [headers] + rows
    # Calculate the widest value in each column (zip(*all_rows) transposes rows into columns)
    widths = [max(len(str(cell)) for cell in col) for col in zip(*all_rows)]
    # Build the separator line:  +--------+--------+
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    # Build the format string:  | {:<8} | {:<8} |   ({:<8} = left-align, 8 chars wide)
    fmt = "| " + " | ".join(f"{{:<{w}}}" for w in widths) + " |"

    lines = [sep, fmt.format(*headers), sep]
    for row in rows:
        lines.append(fmt.format(*row))
    lines.append(sep)
    return "\n".join(lines)


def output_results(macs: list[str], results: list[dict],
                   hostnames: dict[str, str], output_format: str,
                   csv_file: str):
    """Format and print (or write to CSV) the lookup results.

    Matches each input MAC to its API result, resolves device serial numbers
    to hostnames, and outputs one row per location found.
    """
    headers = ["MAC Address", "Switch", "Interface", "VLAN", "Status"]
    rows = []

    # Index results by normalized MAC for fast lookup
    mac_to_result = {}
    for r in results:
        if r.get("search_term"):
            mac_to_result[normalize_mac(r["search_term"])] = r

    # Build output rows in the same order as the input file
    for mac in macs:
        norm = normalize_mac(mac)
        result = mac_to_result.get(norm)

        if result and result.get("locations"):
            for loc in result["locations"]:
                serial = loc["device_id"] or ""
                # Look up hostname from inventory; fall back to serial number
                switch = hostnames.get(serial, serial or "—")
                interface = loc["interface"] or "—"
                vlan = str(loc["vlan"]) if loc["vlan"] is not None else "—"
                rows.append([norm, switch, interface, vlan, "Found"])
        else:
            rows.append([norm, "—", "—", "—", "Not Found"])

    if output_format == "table":
        print(format_table(rows, headers))
    elif output_format == "csv":
        with open(csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        print(f"Results written to {csv_file}")


async def main():
    """Parse CLI arguments, connect to CloudVision, and run the MAC lookup."""
    parser = argparse.ArgumentParser(
        description="Look up MAC address locations in Arista CloudVision"
    )
    parser.add_argument("--server", required=True,
                        help="CloudVision server hostname or IP")
    parser.add_argument("--token-file", required=True,
                        help="Path to service account token file (.tok)")
    parser.add_argument("--mac-file", required=True,
                        help="Path to file with MAC addresses (one per line)")
    parser.add_argument("--output", choices=["table", "csv"], default="table",
                        help="Output format (default: table)")
    parser.add_argument("--csv-file", default="results.csv",
                        help="CSV output file path (default: results.csv)")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS certificate verification")
    args = parser.parse_args()

    # Read the service account token from file
    with open(args.token_file) as f:
        token = f.read().strip()

    macs = read_mac_file(args.mac_file)
    if not macs:
        print("No valid MAC addresses found in input file.", file=sys.stderr)
        sys.exit(1)

    print(f"Looking up {len(macs)} MAC address(es) on {args.server}...")

    # Split "host:port" into separate values — the SDK requires them as separate args
    host, _, port_str = args.server.partition(":")
    port = int(port_str) if port_str else 443

    # Create the CloudVision client using token-based auth
    client = AsyncCVClient.from_token(token, host, port=port, insecure=args.insecure)
    # 'with' opens a gRPC channel that is automatically closed when the block exits
    with client as channel:
        hostnames = await build_hostname_map(channel)
        results = await lookup_macs(channel, macs)

    output_results(macs, results, hostnames, args.output, args.csv_file)


# asyncio.run() starts the async event loop and runs main() to completion
if __name__ == "__main__":
    asyncio.run(main())
