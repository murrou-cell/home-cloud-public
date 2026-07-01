# Triggers an Argo Workflows resize-disk run whenever a VM's disk_size changes.
# Set resize_disk = true on any VM in local.vms to opt in.
# Runs inside the Atlantis pod using the pod's own mounted SA token to call
# the Kubernetes API directly — no Argo Server auth required.

locals {
  resize_disk_vms = {
    for k, v in local.vms : k => v if try(v.resize_disk, false)
  }
}

resource "null_resource" "disk_resize" {
  for_each = local.resize_disk_vms

  triggers = {
    disk_size = each.value.disk_size
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
                "generateName": "resize-disk-",
                "namespace": "argo-workflows"
              },
              "spec": {
                "workflowTemplateRef": {
                  "name": "resize-disk"
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
