locals {
  vms = {
    k3s-master = {
      vm_id             = 100
      description       = "k3s control plane"
      cores             = 2
      memory_mb         = 4096
      disk_size         = 20
      ip_address        = "dhcp"
      bootstrap_ansible = false
    }
    k3s-worker = {
      vm_id             = 101
      description       = "k3s worker — runs all workloads (ArgoCD, Prometheus, Grafana, Ollama)"
      cores             = 4
      memory_mb         = 8192
      disk_size         = 50
      ip_address        = "dhcp"
      bootstrap_ansible = false
    }
    k3s-worker-2 = {
      vm_id             = 102
      description       = "k3s second worker — additional compute capacity"
      cores             = 2
      memory_mb         = 4096
      disk_size         = 30
      ip_address        = "dhcp"
      bootstrap_ansible = true
      node_type         = "worker"
    }
    k3s-ops = {
      vm_id             = 104
      description       = "Dedicated ops node — runs Atlantis and Argo Workflows isolated from application workloads"
      cores             = 2
      memory_mb         = 5120
      disk_size         = 20
      ip_address        = "dhcp"
      bootstrap_ansible = true
      node_type         = "worker"
    }
    k3s-dns = {
      vm_id             = 105
      description       = "Dedicated DNS node — runs cloudflared-doh with hostNetwork on a pinned static IP"
      cores             = 1
      memory_mb         = 512
      disk_size         = 10
      ip_address        = "<YOUR_IP>/24"
      gateway           = "<YOUR_GATEWAY_IP>"
      bootstrap_ansible = true
      node_type         = "dns"
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
        gateway = try(each.value.gateway, null)
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
