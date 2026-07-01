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

printf '[target]\n%s ansible_host=%s ansible_user=ubuntu ansible_ssh_private_key_file=/tmp/id_rsa\n' \
  "${HOST_IP}" "${HOST_IP}" \
  > /tmp/inventory.ini

cd /workspace/repo/ansible
ansible-playbook \
  -i /tmp/inventory.ini \
  playbooks/resize-disk.yml \
  --extra-vars "disk=${DISK} partition=${PARTITION}"
