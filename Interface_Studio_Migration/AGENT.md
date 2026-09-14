# Project Memory

## Purpose

`migrate_profiles.py` migrates interface profiles and interface assignments
from Arista CloudVision's Interface Configuration Studio (ICS) to the Data
Center Interface Configuration Studio (DICS).

The studios use different schemas. The script reads the ICS root JSON, maps
profile fields to the DICS schema, places interfaces in the DICS hierarchy,
and writes the changes to a CloudVision workspace.

## Important behavior

- ICS and DICS data is stored as JSON at the root path (`/`).
- ICS profile collection entries are flat dictionaries.
- ICS resolver entries use `inputs` and `tags.query` wrappers.
- DICS profile field names are the schema field `name` values, not the long
  schema IDs. Verify uncertain fields with `--mode discover`.
- DICS interface hierarchy is DC -> DC-Pod -> Leaf-Domain -> interface.
- Device names parsed from ICS interface tag queries are matched to CloudVision
  device tags labelled `device`, `DC`, `DC-Pod`, and `Leaf-Domain`.
- `portFastEnabled` missing or `None` means enabled; only explicit `False`
  disables it.
- DICS boolean-like string fields use values such as `Yes`/`No`, not Python
  booleans.
- Workspace writes may temporarily return `UNAVAILABLE` or `NOT_FOUND`; use
  `_set_with_retry` for workspace-scoped writes.

## Workspace and cleanup behavior

The normal workflow is create workspace -> write studio inputs -> build ->
optionally submit and execute the resulting change controls.

Successfully placed interface assignments are detached from the ICS in the
workspace. Profile definitions remain in the ICS unless `--cleanup-ics` is
provided. That option clears the ICS `profiles` collection and forces the ICS
root to be written to the workspace.

If a previous migration was submitted/executed, rerunning the script skips
DICS profiles that already exist and finds no already-detached assignments.
If the previous run only created a `workspace-only` workspace, mainline has not
changed and a rerun will still see the original ICS data.

## Studio IDs

Studio discovery streams `StudioSummary` records without a workspace ID. The
script now stops as soon as both IDs are found. For repeated runs, pass the
known IDs to skip the discovery API call entirely:

```bash
python3 migrate_profiles.py \
  --server <host>:<port> \
  --token-file <token-file> \
  --ics-id <ics-studio-id> \
  --dics-id <dics-studio-id>
```

The script prints the IDs and a reusable `--ics-id`/`--dics-id` command line.

## Development

- Python 3.12.
- Dependency: `cloudvision>=1.29.1`.
- The script uses async CloudVision stubs and `AsyncCVClient`.
- Keep ICS-to-DICS mappings explicit and validate schema assumptions with
  discover mode or a workspace build.
- Run `python3 -m py_compile migrate_profiles.py` after Python changes.
