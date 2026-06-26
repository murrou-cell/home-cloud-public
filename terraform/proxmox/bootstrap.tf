# Triggers an Argo Workflows bootstrap after a new VM is provisioned.
# Set bootstrap_ansible = true on any VM in local.vms to enroll it.
# Runs inside the Atlantis pod using the pod's own mounted SA token to call
# the Kubernetes API directly — no Argo Server auth required.

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
        https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/argo-workflows/workflows \
        --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
        -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
        -H "Content-Type: application/json" \
        -d '{
              "apiVersion": "argoproj.io/v1alpha1",
              "kind": "Workflow",
              "metadata": {
                "generateName": "bootstrap-k3s-worker-",
                "namespace": "argo-workflows"
              },
              "spec": {
                "workflowTemplateRef": {
                  "name": "bootstrap-k3s-worker"
                },
                "arguments": {
                  "parameters": [{
                    "name": "host_ip",
                    "value": "${proxmox_virtual_environment_vm.vms[each.key].ipv4_addresses[1][0]}"
                  }]
                }
              }
            }'
    EOT
  }

  provisioner "local-exec" {
    when = destroy
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
                "generateName": "drain-k3s-worker-",
                "namespace": "argo-workflows"
              },
              "spec": {
                "workflowTemplateRef": {
                  "name": "drain-k3s-worker"
                },
                "arguments": {
                  "parameters": [{
                    "name": "node_name",
                    "value": "${each.key}"
                  }]
                }
              }
            }'
    EOT
  }
}
