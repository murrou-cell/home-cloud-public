"""Registry of playbooks (each implements extract_target(payload) and investigate(session, alert_text, target)); investigate.py tries them in order, falling back to agentic.py if none match."""
from . import argocd, host, pod, pvc, workload

PLAYBOOKS = [pod, argocd, pvc, workload, host]
