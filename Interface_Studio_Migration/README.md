# Interface Studio Migration

Automates migrating interface profiles and port assignments from the **Interface Configuration Studio (ICS)** to the **Data Center Interface Configuration Studio (DICS)** in Arista CloudVision-as-a-Service (CVaaS).

## Why

The ICS is an older built-in studio being replaced by the DICS in modern CVaaS deployments. Manually recreating profiles and reassigning them to ports is tedious and error-prone. This script reads profiles from the ICS, maps them to the DICS schema, places interface assignments in the correct DC/Pod/Domain hierarchy, and optionally detaches the migrated assignments from the ICS — all within a single workspace.

## Usage

```bash
python3 migrate_profiles.py \
  --server <cvaas-host>:<port> \
  --token-file <path-to-token-file> \
  --mode <mode>
```

### Required Arguments

| Argument | Description |
|---|---|
| `--server` | CVaaS server address in `host:port` format (port defaults to 443) |
| `--token-file` | Path to a service account token file (`.tok`) |

### Optional Arguments

| Argument | Default | Description |
|---|---|---|
| `--mode` | `workspace-only` | Controls how far the migration proceeds (see Modes below) |
| `--workspace-name` | `ICS to DICS Profile Migration` | Display name for the workspace created in CVaaS |
| `--insecure` | off | Skip TLS certificate verification |
| `--debug` | off | Enable verbose debug output |
| `--cleanup-ics` | off | Remove all interface profile definitions from the ICS in the migration workspace |
| `--ics-id` | auto-discovered | Known ICS studio ID; skips studio discovery for ICS |
| `--dics-id` | auto-discovered | Known DICS studio ID; skips studio discovery for DICS |

### Modes

| Mode | Behavior |
|---|---|
| `discover` | Print both studio schemas and current input data, then exit. No changes are made. Useful for inspecting field names and valid option values before migrating. |
| `workspace-only` | Create a workspace with all migration changes, build it, and leave it open for review in the CVaaS UI. |
| `submit-workspace` | Build and submit the workspace. A change control is created but left open for manual approval. |
| `submit-all` | Build, submit, approve, and execute the change control end-to-end. |

## What Gets Migrated

### Profile Definitions

ICS profiles are mapped to DICS port profiles with the following field conversions:

| ICS Field | DICS Field | Notes |
|---|---|---|
| `name` | `name` | Profile name (used as the collection key) |
| `profileDescription` | `description` | |
| `mode` | `mode` | See Mode Mapping below |
| `speed` | `speed` | ICS dropdown values translated to EOS CLI format (e.g. `1gfull` → `1g`) |
| `accessVlanId` | `vlans.vlans` | Converted to string; used when `allowedVlans` is not set |
| `allowedVlans` | `vlans.vlans` | Trunk allowed VLANs |
| `nativeVlanId` | `vlans.nativeVlan` | |
| `phoneVlanId` | `vlans.phoneVlan` | |
| `portFastEnabled` | `spanningTree.portfast` | `True` or `None` (default) maps to `"edge"`; only explicit `False` disables |
| `ipmtu` | `mtu` | |
| `mlagEnabled` | `portChannel.mlag` | `"Yes"` / `"No"` |
| `lacpEnabled` | `portChannel.portChannelMode` | `true` → `"active"`, `false` → `"on"` |
| `channelGroup` | Port channel enabled | Triggers `portChannel` and `portChannelEnabled` set to `"Yes"` |

### Mode Mapping

| ICS Mode | DICS Mode | Additional Handling |
|---|---|---|
| `access` | `access` | |
| `trunk` | `trunk` | VLANs section: `vlans` (allowed) + `nativeVlan` |
| `phone` | `trunk phone` | Phone section: `trunk` set to `"tagged"`. VLANs section: `nativeVlan` + `phoneVlan` |
| `routed` | *(none)* | No DICS mode set. EOS CLI field populated with `no switchport` and `ip address <ip/cidr>` from the ICS profile |

### Speed Mapping

The ICS uses dropdown shorthand values; the DICS feeds values directly into the EOS `speed` command:

| ICS Value | DICS Value |
|---|---|
| `auto` | `auto` |
| `1gfull` | `1g` |
| `10gfull` | `10g` |
| `25gfull` | `25g` |
| `100full` | `100mfull` |
| `100half` | `100mhalf` |

### Port Channel

When the ICS profile has a channel group, MLAG, or LACP enabled:
- `portChannel` is set to `"Yes"` (makes the section visible in the studio)
- `portChannelEnabled` is set to `"Yes"`
- `portChannelMode` is set to `"active"` (LACP enabled) or `"on"` (no LACP)
- `mlag` is set to `"Yes"` / `"No"`
- LACP fallback mode and timeout are mapped if configured

### Interface Assignments

Each ICS interface assignment (which profile is attached to which port) is placed in the DICS hierarchy:

```
DC -> DC-Pod -> Leaf-Domain -> interface -> adapterDetails.portProfile
```

The script reads device tags (DC, DC-Pod, Leaf-Domain) from CloudVision to determine where each interface belongs. If a device is missing any of these tags, the assignment is skipped with a warning.

### ICS Detachment

After successfully placing an assignment in the DICS, the corresponding interface entry is removed from the ICS data within the same workspace. This prevents the profile from being active in both studios simultaneously.

Use `--cleanup-ics` to also remove all ICS interface profile definitions in the migration workspace. The option is off by default so the source profile definitions remain available for review.

## Prerequisites

- Python 3.12+
- `cloudvision` Python SDK (`pip install 'cloudvision>=1.29.1'`)
- A CVaaS service account token with access to studios, workspaces, and tags
- Devices must have DC, DC-Pod, and Leaf-Domain tags assigned for interface placement

## Examples

Inspect the studio schemas before migrating:

```bash
python3 migrate_profiles.py --server mycloud.arista.io:443 --token-file token.tok --mode discover
```

For repeated runs, provide the known studio IDs to skip studio discovery:

```bash
python3 migrate_profiles.py --server mycloud.arista.io:443 --token-file token.tok --ics-id <ics-studio-id> --dics-id <dics-studio-id>
```

Run the migration and review in CVaaS before committing:

```bash
python3 migrate_profiles.py --server mycloud.arista.io:443 --token-file token.tok --mode workspace-only
```

Run the full migration including change control execution:

```bash
python3 migrate_profiles.py --server mycloud.arista.io:443 --token-file token.tok --mode submit-all
```

Enable debug output for troubleshooting:

```bash
python3 migrate_profiles.py --server mycloud.arista.io:443 --token-file token.tok --debug
```
