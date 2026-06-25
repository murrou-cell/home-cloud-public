#!/usr/bin/env sh
set -e

. /workspace/repo/ansible/scripts/setup-ssh-key.sh

printf '[k3s_master]\n%s ansible_host=%s ansible_user=ubuntu ansible_ssh_private_key_file=/tmp/id_rsa ansible_ssh_common_args="-o StrictHostKeyChecking=no"\n' \
  "${K3S_MASTER_IP}" "${K3S_MASTER_IP}" \
  > /tmp/inventory.ini

cd /workspace/repo/ansible
ansible-playbook \
  -i /tmp/inventory.ini \
  playbooks/drain-k3s-worker.yml \
  -e "node_name=${NODE_NAME}"
