locals {
  vms = {
    k3s-master = {
      vm_id       = 100
      description = "k3s control plane"
      cores       = 2
      memory_mb   = 4096
      disk_size   = 20
      ip_address  = "dhcp"
    }
    k3s-worker = {
      vm_id       = 101
      description = "k3s worker — runs all workloads (ArgoCD, Prometheus, Grafana, Ollama)"
      cores       = 4
      memory_mb   = 12288
      disk_size   = 50
      ip_address  = "dhcp"
    }
    k3s-atlantis = {
      vm_id       = 102
      description = "Dedicated Atlantis runner — isolated from k3s workload nodes so Terraform can modify k3s-master and k3s-worker without disrupting the runner"
      cores       = 1
      memory_mb   = 2048
      disk_size   = 20
      ip_address  = "dhcp"
    }
  }
}

resource "proxmox_virtual_environment_vm" "vms" {
  for_each = local.vms

  name        = each.key
  description = each.value.description
  node_name   = var.proxmox_node
  vm_id       = each.value.vm_id

  clone {
    vm_id = var.vm_template_id
    full  = true
  }

  cpu {
    cores = each.value.cores
    type  = "x86-64-v2-AES"
  }

  memory {
    dedicated = each.value.memory_mb
    floating  = each.value.memory_mb
  }

  disk {
    datastore_id = "local-lvm"
    size         = each.value.disk_size
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
  }

  network_device {
    bridge = var.network_bridge
    model  = "virtio"
  }

  initialization {
    ip_config {
      ipv4 {
        address = each.value.ip_address
      }
    }

    user_account {
      username = "ubuntu"
      keys     = [var.ssh_public_key]
      password = var.vm_password
    }
  }

  operating_system {
    type = "l26"
  }

  agent {
    enabled = true
  }

  on_boot = true
}
