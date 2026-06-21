# home-cloud — Project Forge

A production-grade internal cloud platform built on a single consumer desktop, documented layer by layer as a LinkedIn blog series.

**Hardware:** AMD Ryzen 3 3100 · 24 GB RAM · AMD RX 580 · 1 TB NVMe · Proxmox VE 9

## Stack (in build order)

| Part | Layer | Tag |
|------|-------|-----|
| 1 — Proxmox + Ansible | Bare-metal hypervisor config | `v0.1.0-proxmox-setup` |
| 2 — Terraform | VM provisioning via `bpg/proxmox` | `v0.2.0-terraform-vms` |
| 3 — Ansible + k3s | Kubernetes bootstrap + local tooling | `v0.3.0-k3s-bootstrap` |
| 4 — Argo CD | GitOps delivery layer | coming soon |
| 5 — Atlantis | Self-service Terraform via GitOps | coming soon |

## Principles

- **IaC-first** — every change is codified, nothing manual
- **Git as single source of truth** — no state that lives only on the machine
- **GitOps** — declarative desired state, reconciled automatically
- **Reproducible from scratch** — every step runs cleanly on a fresh Proxmox install

---

## Part 1 — Proxmox Configuration

Switches Proxmox from enterprise (paid) repos to community no-subscription repos. Suppresses the subscription nag. Keeps the host up to date.

```bash
cd ansible
ansible-playbook playbooks/proxmox-setup.yml
ansible-playbook playbooks/system-upgrade.yml
```

**Roles:** `proxmox_community_repos`

---

## Part 2 — Terraform VM Provisioning

Creates a cloud-init ready Ubuntu 24.04 LTS template (VM 9000) with `qemu-guest-agent` baked in via `virt-customize`. Provisions two VMs from that template using the `bpg/proxmox` Terraform provider.

| VM | ID | Cores | RAM | Disk |
|----|----|-------|-----|------|
| k3s-master | 100 | 2 | 4 GB | 20 GB |
| k3s-worker | 101 | 4 | 12 GB | 50 GB |

```bash
# Create Terraform service account + generate tfvars
cd ansible
ansible-playbook playbooks/proxmox-terraform-auth.yml

# Create VM template
ansible-playbook playbooks/proxmox-template.yml

# Provision VMs
cd ../terraform/proxmox
terraform init && terraform apply
```

**Roles:** `proxmox_terraform_auth`, `proxmox_vm_template`

---

## Part 3 — k3s Bootstrap

Bootstraps a k3s cluster across the two provisioned VMs. Installs k9s locally and wires up `~/.kube/config` so kubectl and k9s work from the developer machine without SSH.

```bash
cd ansible
ansible-playbook playbooks/k3s-bootstrap.yml
```

**What it does:**

- `k3s_master` — installs k3s server with `--disable traefik`, waits for Ready, reads the node token
- `k3s_worker` — installs k3s agent, joins the master using the token from hostvars
- `k9s` — fetches kubeconfig from the master, rewrites the server address from `127.0.0.1` to the real IP, writes to `~/.kube/config`, installs k9s binary to `~/.local/bin`

**Roles:** `k3s_master`, `k3s_worker`, `k9s`

---

## Repo structure

```
ansible/
├── ansible.cfg
├── inventory/
│   └── hosts.yml.example
├── playbooks/
│   ├── proxmox-setup.yml
│   ├── proxmox-terraform-auth.yml
│   ├── proxmox-template.yml
│   ├── system-upgrade.yml
│   └── k3s-bootstrap.yml
└── roles/
    ├── proxmox_community_repos/
    ├── proxmox_terraform_auth/
    ├── proxmox_vm_template/
    ├── k3s_master/
    ├── k3s_worker/
    └── k9s/

terraform/
└── proxmox/
    ├── main.tf
    ├── variables.tf
    ├── outputs.tf
    └── terraform.tfvars.example
```

## Prerequisites

- Proxmox VE 9 installed and reachable over SSH
- Ansible (`pip install ansible`)
- Terraform >= 1.6
- SSH key at `~/.ssh/id_ed25519` (injected into VMs via cloud-init)

```bash
cp ansible/inventory/hosts.yml.example ansible/inventory/hosts.yml
# fill in your Proxmox IP and VM IPs
```
