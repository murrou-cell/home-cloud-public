variable "proxmox_endpoint" {
  description = "Proxmox API endpoint, e.g. https://192.168.1.100:8006/"
  type        = string
}

variable "proxmox_api_token" {
  description = "Proxmox API token in format user@realm!tokenid=secret"
  type        = string
  sensitive   = true
}

variable "proxmox_node" {
  description = "Proxmox node name (hostname shown in PVE UI)"
  type        = string
}

variable "ssh_public_key" {
  description = "SSH public key injected into VMs via cloud-init"
  type        = string
}

variable "vm_template_id" {
  description = "VM template ID to clone from (cloud-init ready)"
  type        = number
  default     = 9000
}

variable "network_bridge" {
  description = "Proxmox network bridge for VM NICs"
  type        = string
  default     = "vmbr0"
}

variable "vm_password" {
  description = "Default password for VMs (cloud-init). Use SSH keys in practice."
  type        = string
  sensitive   = true
  default     = null
}
