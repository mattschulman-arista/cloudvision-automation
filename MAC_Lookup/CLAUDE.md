# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python-based automation tooling for Arista CloudVision, using the `cloudvision` Python SDK to interact with CloudVision APIs (streaming telemetry, provisioning, studio actions).

### MAC Lookup Tool

`mac_lookup.py` — reads MAC addresses from a file and queries the CloudVision Endpoint Location API to find the switch hostname and interface where each MAC was learned. 

- **CloudVision APIs used:** `endpointlocation.v1` (Endpoint Location), `inventory.v1` (Device inventory for serial-to-hostname mapping)
- **Auth:** Service account token file (`.tok`)
- **Output:** Table (default) or CSV
- **Important:** `AsyncCVClient.from_token()` takes `host` and `port` as separate parameters — the `--server` argument must be parsed to split them

## Development Environment

- **Python 3.12** via Dev Container (`mcr.microsoft.com/devcontainers/python:3.12`)
- Dependencies installed via pip: `cloudvision>=1.29.1`, `pyyaml`
- No `requirements.txt` yet — dependencies are installed in the devcontainer `postCreateCommand`

## Key Libraries

- **`cloudvision`** (arista-cloudvision): Python SDK for CloudVision's gRPC/REST APIs — resource APIs, streaming subscriptions, change control, configlet/studio interactions
- **`pyyaml`**: YAML parsing for device configs and studio inputs
