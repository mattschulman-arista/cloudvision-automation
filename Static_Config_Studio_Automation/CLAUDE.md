# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python scripts for managing Arista CloudVision Studios via the Resource API (gRPC). Automates configlet assignment, reconciliation, and generic studio input management against CloudVision on-prem or CVaaS.

## Setup

```bash
pip install "cloudvision>=1.29.1" pyyaml
```

Requires Python 3.10+ and a CloudVision service account token saved to a file (e.g., `token.tok`).

A devcontainer config is provided at `.devcontainer/devcontainer.json` using Python 3.12.

## Running the Scripts

Both scripts require `--server <host:port> --token-file <path>` and optionally `--insecure` or `--cert-file`.

**Static configlet management:**
```bash
python3 static_config_studio_automation.py --server host:443 --token-file token.tok --insecure --operation get|set|reconcile|cleanup-reconciled
```

**Generic studio input management:**
```bash
python3 studio_update.py --server host:443 --token-file token.tok --insecure --operation get|set --studio-id <id>
```

There are no tests or build system configured. Linting and formatting use `ruff` (configured in `pyproject.toml`). GitLab CI runs `ruff check` and `ruff format --check` on merge requests via `.gitlab-ci.yml`.

## Architecture

### `static_config_studio_automation.py`
Targets the Static Configuration Studio (`studio-static-configlet`) specifically. Four operations:
- **get**: Exports configlets, containers, devices, and reconcile data as INVENTORY-compatible YAML. Supports `--device-filter` (matches by hostname, resolves to serial via inventory API), `--include-tag-matches` (shows containers whose tag queries match the device's CloudVision tags), `--include-reconcile` (includes RECONCILE_ configlets), `--output-file` (saves to YAML), `--save-configlets` (writes each body to a `.cfg` file), `--configlet-dir` (target directory for saved files), and `--debug` (writes debug JSON files).
- **set**: Creates configlets/assignments from a YAML file (`--inventory-file`) or the hardcoded `INVENTORY` dict, builds a container tree with location tags. Configlet entries support `body` (inline text), `configlet_file` (read from file), or name-only (lookup on CloudVision). Validates the inventory before creating a workspace.
- **reconcile**: Discovers `RECONCILE_`-prefixed configlets, copies their bodies into standalone `<hostname>-reconciled` configlets. Auto-detects where the device is currently assigned (by hash or hostname) and attaches the configlet there, or use `--assignment-root` to specify. Idempotent — appends on re-run rather than replacing. Skips RECONCILE_TREE_KEY and its children.
- **cleanup-reconciled**: Deletes original `RECONCILE_` configlets and cleans up references

### `studio_update.py`
General-purpose tool for any studio. Two operations:
- **get**: Dumps mainline inputs to `<studio-id>-inputs.yaml`
- **set**: Applies inputs from YAML (single or multiple path/inputs pairs), optionally triggers autofill actions, supports workspace sync/rebase with retry, and ordered change control execution

### Shared Patterns
- Both scripts use `AsyncCVClient` from `cloudvision.api` with gRPC streaming (`get_all`, `subscribe`)
- Workspace lifecycle: create → (set inputs) → build → submit → (execute change controls)
- Deterministic UUIDs via `uuid5(NAMESPACE_URL, ...)` for configlets, assignments, and containers in `static_config_studio_automation.py`
- `studio_update.py` uses module-level globals (`studio_id`, `action_id`) set from CLI args before `asyncio.run()`
- `studio_update.py` supports concurrent execution: workspace sync/rebase retry loop and CC ordering (waits for earlier CCs by creation timestamp)
- Deep-merge of studio inputs via `_merge_inputs()` (ported from `studio_update.py`'s `mergeInputs`) to reconstruct nested data from split gRPC responses

### Key CloudVision SDK Modules
- `cloudvision.api.arista.workspace.v1` — workspace lifecycle (create, build, sync, submit)
- `cloudvision.api.arista.studio.v1` — studio inputs
- `cloudvision.api.arista.configlet.v1` — configlets and assignments
- `cloudvision.api.arista.inventory.v1` — device inventory (hostname, serial number, model)
- `cloudvision.api.arista.tag.v2` — device tags (location, role, campus, etc.) and tag assignments
- `cloudvision.api.arista.changecontrol.v1` — change control approval and execution
- `cloudvision.api.arista.action.v1` — autofill action triggers
