# Triggers a prepare-longhorn-disk Argo Workflow when an extra disk is added
# to a VM that is not auto-bootstrapped (bootstrap_ansible = false).
# Bootstrap VMs handle disk prep themselves via bootstrap-k3s-worker.sh.

locals {
  longhorn_trigger_vms = {
    for k, v in local.vms : k => v
    if try(length(v.extra_disks), 0) > 0 && !try(v.bootstrap_ansible, false)
  }
}

resource "null_resource" "longhorn_disk_prep" {
  for_each = local.longhorn_trigger_vms

  triggers = {
    extra_disks = jsonencode(try(each.value.extra_disks, []))
  }

  depends_on = [proxmox_virtual_environment_vm.vms]

  provisioner "local-exec" {
    command = <<-EOT
      curl -sf -X POST \
        https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/argo-workflows/workflows \
        --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
        -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
        -H "Content-Type: application/json" \
        -d '{
              "apiVersion": "argoproj.io/v1alpha1",
              "kind": "Workflow",
              "metadata": {
                "generateName": "prepare-longhorn-disk-",
                "namespace": "argo-workflows"
              },
              "spec": {
                "workflowTemplateRef": {
                  "name": "prepare-longhorn-disk"
                },
                "arguments": {
                  "parameters": [
                    {
                      "name": "host_ip",
                      "value": "${proxmox_virtual_environment_vm.vms[each.key].ipv4_addresses[1][0]}"
                    }
                  ]
                }
              }
            }'
    EOT
  }
}
