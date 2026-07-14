"""Registry of deterministic per-alert-shape investigation playbooks.

Each playbook module implements:
  extract_target(payload) -> dict | None
    Return extracted params if this alert shape matches, else None.
  async def investigate(session, alert_text, target) -> str
    Gather evidence deterministically and return a diagnosis.

investigate.py tries each playbook here in order and uses the first one
whose extract_target() returns non-None. Anything no playbook claims falls
back to agentic.py's open-ended tool-use loop.

To add a new alert shape (node, PVC, Argo CD app, ...): add a new module
implementing that interface, then add it to PLAYBOOKS below.
"""
from . import argocd, host, pod, pvc, workload

PLAYBOOKS = [pod, argocd, pvc, workload, host]
