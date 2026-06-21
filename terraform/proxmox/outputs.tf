output "vm_ids" {
  description = "Proxmox VM IDs keyed by name"
  value       = { for k, v in proxmox_virtual_environment_vm.vms : k => v.vm_id }
}

output "vm_ipv4_addresses" {
  description = "VM IPv4 addresses (populated after QEMU agent starts)"
  value = {
    for k, v in proxmox_virtual_environment_vm.vms :
    k => try(v.ipv4_addresses[1][0], "pending")
  }
}
