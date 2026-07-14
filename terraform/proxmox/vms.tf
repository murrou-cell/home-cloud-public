locals {
  vms = {
    k3s-master = {
      vm_id             = 100
      description       = "k3s control plane node"
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
      memory_mb         = 6144
      disk_size         = 150
      resize_disk       = true
      ip_address        = "dhcp"
      bootstrap_ansible = false
      extra_disks = [
        { size = 100, interface = "virtio1" }
      ]
    }
    k3s-worker-2 = {
      vm_id             = 107
      description       = "k3s worker on pve2 — Longhorn replica + quorum-watchdog"
      cores             = 2
      memory_mb         = 4096
      disk_size         = 20
      ip_address        = "dhcp"
      bootstrap_ansible = true
      node_type         = "worker"
      node_name         = "pve2"
      template_id       = 9001
      cpu_type          = "x86-64-v2"
      extra_disks = [
        { size = 100, interface = "virtio1" }
      ]
    }
    k3s-ops = {
      vm_id             = 104
      description       = "Dedicated ops node — runs Atlantis and Argo Workflows isolated from application workloads"
      cores             = 2
      memory_mb         = 3072
      disk_size         = 20
      ip_address        = "dhcp"
      bootstrap_ansible = true
      node_type         = "worker"
    }
    k3s-dns = {
      vm_id             = 105
      description       = "Dedicated DNS + VPN node — runs cloudflared-doh and warp-vpn with hostNetwork on a pinned static IP"
      cores             = 1
      memory_mb         = 1536
      disk_size         = 10
      ip_address        = "<YOUR_DNS_VM_IP>/24"
      gateway           = "<YOUR_GATEWAY_IP>"
      bootstrap_ansible = true
      node_type         = "dns"
    }
    k3s-gpu = {
      vm_id             = 106
      description       = "GPU-passthrough worker — RX 580 for llama.cpp (Vulkan backend), Claude cost-gate model"
      cores             = 2
      memory_mb         = 7168
      disk_size         = 32
      ip_address        = "dhcp"
      bootstrap_ansible = true
      node_type         = "worker"
      machine           = "q35"
      # Raw hostpci `id=` requires root username/password auth; the
      # least-privileged terraform@pve API token can only reference PCI
      # devices via cluster resource mappings (see proxmox_gpu_pci_mapping).
      # rombar=false: this VM only needs the GPU for headless compute
      # (Vulkan), never its video output, and exposing the ROM made SeaBIOS
      # hang executing the card's video BIOS during POST (silent console,
      # pegged vCPU, no DHCP lease -- never reached the OS at all).
      hostpci = [
        { mapping = "gpu-rx580-vga", pcie = true, rombar = false },
        { mapping = "gpu-rx580-audio", pcie = true, rombar = false }
      ]
    }
  }
}

resource "proxmox_virtual_environment_vm" "vms" {
  for_each = local.vms

  name        = each.key
  description = each.value.description
  node_name   = try(each.value.node_name, var.proxmox_node)
  vm_id       = each.value.vm_id
  machine     = try(each.value.machine, null)

  clone {
    vm_id = try(each.value.template_id, var.vm_template_id)
    full  = true
  }

  cpu {
    cores = each.value.cores
    type  = try(each.value.cpu_type, "x86-64-v2-AES")
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

  dynamic "disk" {
    for_each = try(each.value.extra_disks, [])
    content {
      datastore_id = "local-lvm"
      size         = disk.value.size
      interface    = disk.value.interface
      iothread     = true
      discard      = "on"
    }
  }

  dynamic "hostpci" {
    for_each = { for idx, pci in try(each.value.hostpci, []) : idx => pci }
    content {
      device  = "hostpci${hostpci.key}"
      id      = try(hostpci.value.id, null)
      mapping = try(hostpci.value.mapping, null)
      pcie    = try(hostpci.value.pcie, true)
      rombar  = try(hostpci.value.rombar, true)
    }
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
