#!/usr/bin/env sh
set -e

. /workspace/repo/ansible/scripts/setup-ssh-key.sh

echo "Waiting for SSH on ${PVE2_IP}..."
until ssh \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout=5 \
  -o BatchMode=yes \
  -i /tmp/id_rsa \
  root@"${PVE2_IP}" true 2>/dev/null; do
  sleep 10
done
echo "SSH ready."

printf '[proxmox_hosts]\npve ansible_host=%s ansible_user=root ansible_ssh_private_key_file=/tmp/id_rsa\n\n[proxmox_new_nodes]\npve2 ansible_host=%s ansible_user=root ansible_ssh_private_key_file=/tmp/id_rsa\n' \
  "${PVE_IP}" "${PVE2_IP}" > /tmp/inventory.ini

cd /workspace/repo/ansible
ansible-playbook \
  -i /tmp/inventory.ini \
  playbooks/proxmox-cluster-join.yml \
  -e "proxmox_primary_node_ip=${PVE_IP}" \
  -e "proxmox_primary_node_password=${PVE_PASSWORD}" \
  -e "proxmox_new_node_password=${PVE2_PASSWORD}"
