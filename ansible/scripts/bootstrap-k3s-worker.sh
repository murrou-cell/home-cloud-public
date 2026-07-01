#!/usr/bin/env sh
set -e

. /workspace/repo/ansible/scripts/setup-ssh-key.sh

echo "Waiting for SSH on ${HOST_IP}..."
until ssh \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout=5 \
  -o BatchMode=yes \
  -i /tmp/id_rsa \
  ubuntu@"${HOST_IP}" true 2>/dev/null; do
  sleep 10
done
echo "SSH ready."

NODE_TYPE="${NODE_TYPE:-worker}"

K3S_TOKEN=$(ssh \
  -o StrictHostKeyChecking=no \
  -i /tmp/id_rsa \
  ubuntu@"${K3S_MASTER_IP}" \
  "sudo cat /var/lib/rancher/k3s/server/node-token" | tr -d '\r\n')
echo "k3s token fetched."

if [ "${NODE_TYPE}" = "dns" ]; then
  printf '[k3s_master]\n%s ansible_host=%s ansible_user=ubuntu ansible_ssh_private_key_file=/tmp/id_rsa k3s_node_token=%s\n\n[k3s_workers]\n%s ansible_host=%s ansible_user=ubuntu ansible_ssh_private_key_file=/tmp/id_rsa\n\n[k3s_dns]\n%s ansible_host=%s ansible_user=ubuntu ansible_ssh_private_key_file=/tmp/id_rsa\n' \
    "${K3S_MASTER_IP}" "${K3S_MASTER_IP}" "${K3S_TOKEN}" \
    "${HOST_IP}" "${HOST_IP}" \
    "${HOST_IP}" "${HOST_IP}" \
    > /tmp/inventory.ini
  LIMIT="k3s_workers,k3s_dns"
else
  printf '[k3s_master]\n%s ansible_host=%s ansible_user=ubuntu ansible_ssh_private_key_file=/tmp/id_rsa k3s_node_token=%s\n\n[k3s_workers]\n%s ansible_host=%s ansible_user=ubuntu ansible_ssh_private_key_file=/tmp/id_rsa\n' \
    "${K3S_MASTER_IP}" "${K3S_MASTER_IP}" "${K3S_TOKEN}" \
    "${HOST_IP}" "${HOST_IP}" \
    > /tmp/inventory.ini
  LIMIT="k3s_workers"
fi

cd /workspace/repo/ansible
ansible-playbook \
  -i /tmp/inventory.ini \
  playbooks/k3s-bootstrap.yml \
  --limit "${LIMIT}"

if [ "${NODE_TYPE}" = "storage" ]; then
  ansible-playbook \
    -i /tmp/inventory.ini \
    playbooks/mount-data-disk.yml \
    --limit "${LIMIT}"
fi

if [ "${NODE_TYPE}" = "worker" ]; then
  ansible-playbook \
    -i /tmp/inventory.ini \
    playbooks/prepare-longhorn-disk.yml \
    --limit "${LIMIT}"
fi
