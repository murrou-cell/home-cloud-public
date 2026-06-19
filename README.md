# home-cloud — Part 1: Proxmox Configuration with Ansible

This repository contains the Ansible automation used to configure a bare-metal Proxmox VE host as the foundation of a home cloud platform.

Part of a series building a production-inspired platform engineering environment on a single-node homelab.

## What this does

- Switches Proxmox from the enterprise (paid) repositories to the community no-subscription repositories
- Suppresses the subscription nag popup in the Proxmox web UI
- Keeps the system up to date via a dedicated upgrade playbook

## Stack

- **Proxmox VE 9** (Debian Trixie)
- **Ansible**

## Structure

```
ansible/
├── ansible.cfg                          # roles_path and default inventory
├── inventory/
│   └── hosts.yml.example                # inventory template
├── playbooks/
│   ├── proxmox-setup.yml                # configure Proxmox repositories + nag fix
│   └── system-upgrade.yml               # apt update + full-upgrade
└── roles/
    └── proxmox_community_repos/         # role: switch to community repos
        ├── defaults/main.yml
        ├── handlers/main.yml
        ├── meta/main.yml
        └── tasks/main.yml
```

## Usage

```bash
cp ansible/inventory/hosts.yml.example ansible/inventory/hosts.yml
# edit hosts.yml with your Proxmox host IP and credentials

cd ansible
ansible-playbook playbooks/proxmox-setup.yml
ansible-playbook playbooks/system-upgrade.yml
```

## Role variables

| Variable | Default | Description |
|----------|---------|-------------|
| `pve_debian_suite` | `trixie` | Debian suite name matching your PVE version |
| `pve_enable_ceph_no_sub` | `false` | Also configure Ceph community repo |
| `pve_suppress_nag` | `true` | Patch the subscription nag popup |

## Series

| Part | Topic | Repo |
|------|-------|------|
| 1 — Proxmox + Ansible | Configure bare-metal PVE host | this repo |
| 2 — Terraform | Provision VMs declaratively | coming soon |
| 3 — Ansible + k3s | Bootstrap Kubernetes | coming soon |
| 4 — Argo CD | GitOps layer | coming soon |
