# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this directory.

## Project Overview

`migrate_profiles.py` automates migrating interface profiles and port assignments from the **Interface Configuration Studio (ICS)** to the **Data Center Interface Configuration Studio (DICS)** in Arista CVaaS.

### Key Concepts

- **ICS and DICS have completely different schemas.** You cannot copy inputs between them. The script performs JSON-level field mapping.
- **Studio inputs are JSON blobs** stored at paths within a studio. Both studios store all data at the root path (`/`).
- **The DICS uses short JSON field names** derived from the schema field's `name` attribute, NOT the long schema field IDs. For example, the schema field `portProfileName` has JSON key `name`, and `portProfilePortChannelMlag` has JSON key `mlag`. Use `--mode discover` to see the `[name=...]` mappings.
- **Sub-group field names strip the parent prefix.** Within the `portChannel` group, member `portProfilePortChannelMlag` becomes `mlag`. Within `adapterDetails`, member `adapterPortProfile` becomes `portProfile`.
- **Profile collection entries are flat dicts** (no `{"inputs": {...}}` wrapper), unlike resolver entries which use `{"inputs": {...}, "tags": {"query": "..."}}`.
- **The ICS defaults `portFastEnabled` to enabled.** A `None`/missing value means portfast is ON. Only an explicit `False` means disabled.
- **Boolean-like STRING fields** in the DICS use option values like `"Yes"/"No"` or EOS keywords like `"edge"/"network"`, not `"true"/"false"`.

### CloudVision APIs Used

- **Studio APIs** (`cloudvision.api.arista.studio.v1`): Read/write studio inputs and schemas via aristaproto service stubs (e.g., `InputsServiceStub`, `InputsConfigServiceStub`)
- **Workspace APIs** (`cloudvision.api.arista.workspace.v1`): Create, build, and submit workspaces
- **Change Control APIs** (`cloudvision.api.arista.changecontrol.v1`): Approve and execute change controls
- **Tag APIs** (`cloudvision.api.arista.tag.v2`): Read device tag assignments (DC, DC-Pod, Leaf-Domain) to place interfaces in the DICS hierarchy
- **Auth**: `AsyncCVClient.from_token()` with `host` and `port` as separate parameters

### Important Patterns

- **Workspace writes may need retries.** After creation, CVaaS can return `UNAVAILABLE` or `NOT_FOUND` briefly. The `_set_with_retry` helper handles this.
- **The DICS hierarchy** is DC → DC-Pod → Leaf-Domain → interface. JSON path: `dc[].inputs.networkDetails.dcPod[].inputs.networkPodDetails.leafDomain[].inputs.accessPodDetails.interfaces[]`.
- **Device names from ICS** (e.g., `3Site-C-SLEAF5` from tag query `interface:Ethernet6@3Site-C-SLEAF5`) are matched to device tags via the `device` tag label.

## Development Environment

- **Python 3.12** via Dev Container
- Dependencies: `cloudvision>=1.29.1`
- Uses async Python with `asyncio.run()` and `AsyncCVClient`
