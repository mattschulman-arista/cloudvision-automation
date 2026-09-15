# Static Management IP Automation

`manage_static_mgmt_ip.py` reads device management addressing from a CSV and
writes the `deviceAddressing` collection in CloudVision's Management
Connectivity Studio inside a workspace.

The `device` column must contain the CloudVision device hostname. The script looks up each hostname in inventory and writes the returned serial number in the resolver query.

The `ipv4Address` column is mandatory and must be a valid IPv4 address in CIDR notation (for example, `192.0.2.10\/24`). The `defaultGateway` column must be a valid plain IPv4 address (for example, `192.0.2.1`).

The CSV header must be:

```text
device,mgmtVRF,enabled,ipv4Address,defaultGateway,additionalInterfaces
```

`additionalInterfaces` may be empty, a comma-separated list of interface names, or a JSON list of objects such as `[{"interfaceName":"Management1\/1","ipv4Address":"192.0.2.10\/24"}]`. Each CSV row is written as a device-tagged resolver entry using the nested Management Connectivity schema.

## --mode options

`workspace-only` - Dry run (Leaves workspace open):

```bash
python3 manage_static_mgmt_ip.py --server cloudvision.example.com \
  --token-file service-account.tok --input-file devices.csv \
  --mode workspace-only
```

Other values for --mode:

`submit-workspace` submits the workspace and leaves its change control open;
`submit-all` also approves the resulting change control.  The script does not
start execution of the change control, so execution remains a deliberate
CloudVision workflow step.

## Workspace name

Use `--workspace-name "<string>"` to set the CloudVision workspace display name. If omitted, the default is `Static Management IP Automation`.

```bash
python3 manage_static_mgmt_ip.py --server cloudvision.example.com \
  --token-file service-account.tok --input-file devices.csv \
  --mode workspace-only --workspace-name "Building 1 Management IPs"
```

Install the SDK with `pip install 'cloudvision>=1.29.1'`.
