# Triggers an Argo Workflows bootstrap after a new VM is provisioned.
# Set bootstrap_ansible = true on any VM in local.vms to enroll it.
# Runs inside the Atlantis pod, which has in-cluster DNS to reach Argo Workflows.
# ARGO_TOKEN is injected into the Atlantis pod via environmentSecrets (atlantis-secrets).

locals {
  bootstrap_vms = {
    for k, v in local.vms : k => v if v.bootstrap_ansible
  }
}

resource "null_resource" "ansible_bootstrap" {
  for_each = local.bootstrap_vms

  triggers = {
    vm_id = proxmox_virtual_environment_vm.vms[each.key].vm_id
  }

  provisioner "local-exec" {
    command = <<-EOT
      curl -sf -X POST \
        http://argo-workflows-server.argo-workflows.svc.cluster.local:2746/api/v1/workflows/argo-workflows/submit \
        -H "Authorization: Bearer $ARGO_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
              "resourceKind": "WorkflowTemplate",
              "resourceName": "bootstrap-k3s-worker",
              "submitOptions": {
                "parameters": ["host_ip=${proxmox_virtual_environment_vm.vms[each.key].ipv4_addresses[1][0]}"]
              }
            }'
    EOT
  }

  provisioner "local-exec" {
    when = destroy
    command = <<-EOT
      curl -sf -X POST \
        http://argo-workflows-server.argo-workflows.svc.cluster.local:2746/api/v1/workflows/argo-workflows/submit \
        -H "Authorization: Bearer $ARGO_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
              "resourceKind": "WorkflowTemplate",
              "resourceName": "drain-k3s-worker",
              "submitOptions": {
                "parameters": ["node_name=${each.key}"]
              }
            }'
    EOT
  }
}
