#!/usr/bin/env bash
# Run from anywhere — paths are relative to the repo root.
set -euo pipefail

# ── Required env vars ────────────────────────────────────────────────────────
# Export these before running or pass them inline.
# Use single quotes to prevent the shell interpreting special characters (!, @, etc.):
#
#   PRIVATE_REPO_PASSWORD='ghp_...' \
#   BW_CLIENTID='...'               \
#   BW_CLIENTSECRET='...'           \
#   BW_PASSWORD='...'               \
#   ./scripts/bootstrap-argocd.sh
#
# PRIVATE_REPO_PASSWORD — GitHub PAT with repo:read scope for murrou-cell/home-cloud
# BW_CLIENTID           — Bitwarden API client ID  (account → Security → API key)
# BW_CLIENTSECRET       — Bitwarden API client secret
# BW_PASSWORD           — Bitwarden master password (needed to unlock the vault)
# ─────────────────────────────────────────────────────────────────────────────

: "${PRIVATE_REPO_PASSWORD:?Set PRIVATE_REPO_PASSWORD to a GitHub PAT with repo:read scope}"
: "${BW_CLIENTID:?Set BW_CLIENTID to your Bitwarden API client ID}"
: "${BW_CLIENTSECRET:?Set BW_CLIENTSECRET to your Bitwarden API client secret}"
: "${BW_PASSWORD:?Set BW_PASSWORD to your Bitwarden master password}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "${REPO_ROOT}/ansible"

ansible-playbook playbooks/argocd-bootstrap.yml \
  -e "private_repo_password=${PRIVATE_REPO_PASSWORD}" \
  -e "bitwarden_client_id=${BW_CLIENTID}" \
  -e "bitwarden_client_secret=${BW_CLIENTSECRET}" \
  -e "bitwarden_password=${BW_PASSWORD}"
