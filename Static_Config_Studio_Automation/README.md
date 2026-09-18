# Static Configuration Studio API Tools

Python scripts for managing CloudVision Static Configuration Studio via the Resource API. These tools automate configlet assignment, reconciliation, and generic studio input management against CloudVision on-prem or CVaaS.

This script is based on the studio_static_config_simple.py python script on the Arista cloudvision-python repo here: https://github.com/aristanetworks/cloudvision-python/tree/trunk/examples/resources/studio


## Prerequisites

- Python 3.10+
- CloudVision Python SDK:
  ```bash
  pip install "cloudvision>=1.29.1" pyyaml
  ```
- A CloudVision service account token (generated from Settings > Service Accounts in the CloudVision UI). Save it to a file (e.g., `token.tok`).

## Scripts

### `static_config_studio_automation.py`

Manages the **Static Configuration Studio** (`studio-static-configlet`). Supports four operations:

#### `get` — Export configlets, assignments, and reconcile data as YAML

```bash
# Show everything:
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation get

# Filter by device hostname and save to file:
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation get --device-filter leaf1 --output-file leaf1.yaml

# Include containers whose tag queries match the device:
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation get --device-filter leaf1 --include-tag-matches
```

Outputs an INVENTORY-compatible YAML dict with up to three sections:
- **containers** — tag-based assignment containers with their query, parent hierarchy, and attached configlets (including body)
- **devices** — per-device assignments with device ID, parent container, and attached configlets (including body)
- **reconcile** — *(only with `--include-reconcile`)* RECONCILE_-prefixed configlets with device hash, hostname, and body

```bash
# Save configlet bodies as individual .cfg files:
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation get --save-configlets --configlet-dir my_configlets

# Include reconcile configlets in output:
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation get --include-reconcile
```

**Get options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--device-filter` | *(none)* | Filter by substring match on device hostname. Resolves hostnames to serial numbers via the CloudVision inventory API. |
| `--output-file` | *(none)* | Save output to this YAML file (in addition to printing). |
| `--include-tag-matches` | `false` | With `--device-filter`: also show containers whose tag query matches the device's CloudVision tags (e.g., `Campus:HQ`, `Role:Spine`). Also includes `device:*` entries. |
| `--include-reconcile` | `false` | Include RECONCILE_-prefixed configlets in the output. |
| `--save-configlets` | `false` | Save each configlet body to a `<name>.cfg` file and replace the body in YAML output with a `configlet_file` path reference. |
| `--configlet-dir` | `configlets` | Directory to save configlet files when using `--save-configlets`. Created if it doesn't exist. |
| `--debug` | `false` | Write debug JSON files (`debug_studio_inputs.json`, `debug_assignments.json`, `debug_configlets.json`) for troubleshooting. |

#### `set` — Assign configlets from an inventory file or hardcoded dict

Load the inventory from a YAML file (recommended) or edit the `INVENTORY` dict at the top of the script:

```bash
# From a YAML file (output from 'get' can be used as input):
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation set --inventory-file inventory.yaml

# From the hardcoded INVENTORY dict (fallback):
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation set
```

The inventory supports three configlet modes (checked in order):
- `{"name": "x", "body": "..."}` — create a configlet with inline body text
- `{"name": "x", "configlet_file": "path"}` — create a configlet from a local file
- `{"name": "x"}` — reference an existing configlet by display name on CloudVision

The inventory is validated before creating a workspace — if any configlet entry has no `body`, no `configlet_file`, or references a name not found on CloudVision, the script exits with an error and no workspace is opened.

Container paths use `/` for nesting (e.g., `US/DC1`). Devices are placed under a container with the `container` key. Location tags are automatically created and assigned to devices so container queries match.

The output from `get --save-configlets` produces an inventory file with `configlet_file` references that can be directly used as input for `set`.

| Option | Default | Description |
|--------|---------|-------------|
| `--inventory-file` | *(none)* | YAML file containing the inventory dict (overrides hardcoded `INVENTORY`). |
| `--build-only` | `false` | Validate without submitting. |

#### `reconcile` — Copy reconciled configlets into standalone configlets

Discovers all CloudVision-generated reconciled configlets (IDs starting with `RECONCILE_`), fetches their actual EOS CLI config body, creates new standalone configlets from that content, and assigns them to the appropriate devices under a specified assignment root. This gives you ownership of the config so you can later delete the original reconciled configlets.

The operation is **idempotent and incremental**:
- First run: creates `<hostname>-reconciled` configlets and device assignments
- Subsequent runs: if a device is reconciled again, the new config is **appended** to the existing `-reconciled` configlet (not replaced), preserving all previously captured config
- If nothing has changed, the operation exits with "Nothing to do"

The operation also cleans up:
- Removes `RECONCILE_` references from existing assignments
- Deletes orphaned duplicate root assignments
- Updates `configletAssignmentRoots` to remove stale entries

**Add reconciled configs to existing device assignments (e.g., under `avd-configlets`):**

```bash
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation reconcile \
    --assignment-root avd-configlets \
    --device-filter dc1-
```

**Create new device assignments under a container (e.g., `DC1`):**

```bash
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation reconcile \
    --assignment-root DC1 \
    --device-filter dc1-
```

**Dry run first, then submit:**

```bash
# Validate (build only):
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation reconcile \
    --assignment-root DC1 --device-filter dc1- \
    --build-only

# Full run (build + submit):
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation reconcile \
    --assignment-root DC1 --device-filter dc1-
```

**Reconcile options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--assignment-root` | *(auto-detect)* | Assignment root ID or display name. If not specified, the script finds where the device is already assigned and attaches there. If the device isn't in any container, attaches to the studio root. |
| `--device-filter` | *(required)* | Only process reconciled configlets whose name contains this string (e.g., `dc1-`). **Required** for `reconcile`. |
| `--build-only` | `false` | Stop after build validation, don't submit. |

**How it works:**

1. Streams all configlets from mainline with `include_body=True` and filters for IDs starting with `RECONCILE_`.
2. Filters by `--device-filter` on the configlet display name.
3. Finds where each device is currently assigned:
   - If `--assignment-root` is specified: uses that container for all devices.
   - Otherwise: searches all assignments for the device (by serial hash or hostname), skipping RECONCILE_TREE_KEY. Prefers assignments with existing non-RECONCILE configlets.
   - If the device isn't found anywhere: attaches to the studio root.
4. For each reconciled configlet:
   - If the device has an existing assignment and `<hostname>-reconciled` already exists and is up to date: skips.
   - If `<hostname>-reconciled` already exists but has new config: **appends** the new config.
   - If the device has an existing assignment but no `-reconciled` configlet: creates one and adds it.
   - If the device has no assignment: creates a new assignment with the reconciled configlet.
5. Cleans up orphaned duplicate assignments and stale `configletAssignmentRoots` entries.
6. Builds and optionally submits the workspace.

#### `cleanup-reconciled` — Delete original reconciled configlets

After running `reconcile` to copy reconciled configs into standalone configlets, use this operation to delete the original `RECONCILE_` configlets, remove `RECONCILE_TREE_KEY` from the studio roots, and clean up all assignments that referenced them.

```bash
# Dry run:
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation cleanup-reconciled \
    --build-only

# Full run:
python3 static_config_studio_automation.py \
    --server www.cv-prod-us-4.arista.io:443 \
    --token-file token.tok --insecure \
    --operation cleanup-reconciled
```

**What it does:**

1. Finds all configlets with IDs starting with `RECONCILE_`.
2. Finds all assignments that reference those configlets:
   - Assignments that *only* contain `RECONCILE_` refs are deleted entirely.
   - Assignments with a mix of `RECONCILE_` and other configlets have the `RECONCILE_` refs removed.
3. Removes `RECONCILE_TREE_KEY` from `configletAssignmentRoots`.
4. Builds and optionally submits the workspace.

### Typical workflow

```bash
ARGS="--server www.cv-prod-us-4.arista.io:443 --token-file token.tok --insecure"

# 1. Onboard devices and reconcile configs in CloudVision UI

# 2. Check current state
python3 static_config_studio_automation.py $ARGS --operation get

# 3. Copy reconciled configlets into a container (e.g., DC1)
#    Dry run first:
python3 static_config_studio_automation.py $ARGS --operation reconcile \
    --assignment-root DC1 --device-filter dc1- --build-only
#    Then submit:
python3 static_config_studio_automation.py $ARGS --operation reconcile \
    --assignment-root DC1 --device-filter dc1-

# 4. Delete original reconciled configlets
python3 static_config_studio_automation.py $ARGS \
    --operation cleanup-reconciled
```

### Pipeline automation

Chain reconcile and cleanup in a single pipeline. Each operation is idempotent — if there's nothing to do, it exits cleanly.

```bash
#!/bin/bash
ARGS="--server www.cv-prod-us-4.arista.io:443 --token-file token.tok --insecure"

# Copy reconciled configs into standalone configlets under DC1
python3 static_config_studio_automation.py $ARGS \
    --operation reconcile \
    --assignment-root DC1 --device-filter dc1- || exit 1

# Delete original RECONCILE_ configlets and remove RECONCILE_TREE_KEY
python3 static_config_studio_automation.py $ARGS \
    --operation cleanup-reconciled
```

This pipeline is safe to run repeatedly (e.g., via cron or CI). On each run it will:
1. Pick up any new reconciled configs and append them to existing `-reconciled` configlets
2. Clean up the originals

If no new reconciled configs exist, both operations exit with nothing to do.

