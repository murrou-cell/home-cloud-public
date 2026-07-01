#!/usr/bin/env bash
set -euo pipefail

PVE2_IP="${PVE2_IP:-<YOUR_PVE2_IP>}"

/scripts/setup-ssh-key.sh

cat > /tmp/inventory.ini <<EOF
[proxmox_hosts]
pve2 ansible_host=${PVE2_IP} ansible_user=root ansible_ssh_private_key_file=/tmp/id_rsa ansible_ssh_common_args='-o StrictHostKeyChecking=no'
EOF

cd /ansible
ansible-playbook -i /tmp/inventory.ini playbooks/proxmox-data-pool.yml
