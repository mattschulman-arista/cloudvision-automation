# MAC Lookup

Look up MAC address locations in Arista CloudVision. Given a list of MAC addresses, queries the CloudVision Endpoint Location API and returns the switch hostname and interface where each MAC was learned.

## Prerequisites

- Python 3.12+
- Access to an Arista CloudVision instance (on-prem or CVaaS)
- A CloudVision service account token (`.tok` file)

### Install dependencies

```bash
pip install 'cloudvision>=1.29.1'
```

## Usage

### 1. Create a MAC address file

Create a text file with one MAC address per line. Supported formats:

```
e4:d1:24:b5:7b:ff
d4-e5-c9-05-34-59
aabb.ccdd.eeff
```

Lines starting with `#` and blank lines are ignored.

### 2. Run the lookup

```bash
python3 mac_lookup.py \
  --server <cloudvision-host>:<port> \
  --token-file <path-to-token>.tok \
  --mac-file macs.txt \
  --output table
```


### CLI options

| Option | Required | Default | Description |
|---|---|---|---|
| `--server` | Yes | | CloudVision server (`host` or `host:port`) |
| `--token-file` | Yes | | Path to service account token file |
| `--mac-file` | Yes | | Path to file with MAC addresses |
| `--output` | No | `table` | Output format: `table` or `csv` |
| `--csv-file` | No | `results.csv` | CSV output file path |
| `--insecure` | No | | Skip TLS certificate verification |

### Example output

```
+-------------------+------------+-----------------+------+--------+
| MAC Address       | Switch     | Interface       | VLAN | Status |
+-------------------+------------+-----------------+------+--------+
| e4:d1:24:b5:7b:ff | leaf-1a    | Ethernet3       | 100  | Found  |
| d4:e5:c9:05:34:59 | —          | —               | —    | Not Found |
+-------------------+------------+-----------------+------+--------+
```

## How it works

1. Reads and validates MAC addresses from the input file
2. Connects to CloudVision using a service account token
3. Fetches the device inventory to build a serial-number-to-hostname map
4. Queries the Endpoint Location API for each MAC address
5. Joins the results and outputs a table or CSV with the switch name, interface, and VLAN
