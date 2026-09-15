# CloudVision Automation

A collection of Python tools for automating tasks with Arista CloudVision using the [`cloudvision`](https://pypi.org/project/cloudvision/) SDK.

## Projects

| Project | Description |
|---|---|
| [MAC_Lookup](MAC_Lookup/) | Look up MAC address locations across your network. Given a list of MAC addresses, queries the CloudVision Endpoint Location API and returns the switch hostname, interface, and VLAN where each MAC was learned. Outputs results as an ASCII table or CSV. |
| [Interface_Studio_Migration](Interface_Studio_Migration/) | Migrate interface profiles and port assignments from the Interface Configuration Studio (ICS) to the Data Center Interface Configuration Studio (DICS) in CVaaS. Maps profile fields between the two studio schemas, places interfaces in the correct DC/Pod/Domain hierarchy, and detaches migrated assignments from the ICS. |
|[Static_Mgmt_IP_Automation](Static_Mgmt_IP_Automation/) | Read in a CSV file of device and static Mgmt IP address/GW and VRF info to populate the Static Addressing section of the Management Connectivity Studio.|

## Prerequisites

- Python 3.12+
- Access to an Arista CloudVision instance (on-prem or CVaaS)
- A CloudVision service account token (`.tok` file)

## Getting Started

### Dev Container (recommended)

This repository includes a Dev Container configuration for VS Code / GitHub Codespaces. Opening the repo in a Dev Container automatically installs Python 3.12 and all dependencies.

### Manual setup

Install the Python dependencies:

```bash
pip install 'cloudvision>=1.29.1' pyyaml
```

Then navigate to a project folder and follow its README for usage instructions.
