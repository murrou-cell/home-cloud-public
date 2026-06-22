# home-cloud

Personal internal cloud platform — production-grade architecture on a single-node homelab.

## Architecture

| Layer | Technology | Status |
|-------|-----------|--------|
| 0 — Bare metal | Proxmox VE | Done |
| 1 — Virtualization | Proxmox VMs | In progress |
| 2 — IaC provisioning | Terraform (bpg/proxmox) | In progress |
| 3 — Config management | Ansible | Planned |
| 4 — Kubernetes | k3s | Planned |
| 5 — GitOps | Argo CD | Planned |
| 6 — Platform services | Prometheus · Grafana · n8n | Planned |
| 7 — Local AI | Ollama · Open WebUI | Planned |

## Hardware

- CPU: AMD Ryzen 3 3100 (4c/8t)
- RAM: 24 GB
- GPU: AMD Radeon RX 580 4 GB (local LLM inference)
- Storage: 1 TB NVMe SSD

## Prerequisites

Before running Terraform you need:

1. **A cloud-init–ready VM template** on your Proxmox node — see `docs/proxmox-template.md`
2. **A Proxmox API token** with appropriate permissions — see `docs/proxmox-api-token.md`
3. **Terraform ≥ 1.6** installed locally

## Getting started

```bash
cd terraform/proxmox
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars with your values
terraform init
terraform plan
terraform apply
```

## Repository layout

```
terraform/    # VM provisioning (Layer 2)
ansible/      # OS bootstrap + k3s install (Layer 3)
kubernetes/   # k3s manifests + ArgoCD apps (Layers 4-6)
docs/         # Setup guides and architecture notes
```
