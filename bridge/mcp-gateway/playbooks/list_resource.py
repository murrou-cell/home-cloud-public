"""Direct /ask-only intent: "how many/which X exist" - one generic intent instead of a bespoke playbook per resource type, so a new kind is a one-line RESOURCES entry."""
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from common import MAX_TOOL_RESULT_CHARS, PROXMOX_MCP_URL, call_tool_text, chat_completion, load_prompt

NAME = "list_resource"

# resource keyword -> (tool, args); fixed Kubernetes/Proxmox resource *kinds*, not homelab-specific facts, so not the hardcoding to avoid.
K8S_RESOURCES = {
    "nodes": ("resources_list", {"apiVersion": "v1", "kind": "Node"}),
    "namespaces": ("namespaces_list", {}),
    "pods": ("pods_list", {}),
    "pvcs": ("resources_list", {"apiVersion": "v1", "kind": "PersistentVolumeClaim"}),
    "deployments": ("resources_list", {"apiVersion": "apps/v1", "kind": "Deployment"}),
    "daemonsets": ("resources_list", {"apiVersion": "apps/v1", "kind": "DaemonSet"}),
    "statefulsets": ("resources_list", {"apiVersion": "apps/v1", "kind": "StatefulSet"}),
    "jobs": ("resources_list", {"apiVersion": "batch/v1", "kind": "Job"}),
    "poddisruptionbudgets": ("resources_list", {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget"}),
    "argocd_apps": ("resources_list", {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application"}),
}


def _count_table_rows(text):
    """kubernetes-mcp-server's list tools render one header line + one row per resource; count on the full untruncated text to avoid silently undercounting a large list."""
    lines = [line for line in text.splitlines() if line.strip()]
    return max(len(lines) - 1, 0)


def _sample(text, max_chars=MAX_TOOL_RESULT_CHARS):
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... (truncated for length, see the exact count above)"


async def investigate(session, alert_text, target):
    resource = target["resource"]
    if resource == "proxmox_hosts":
        # Separate MCP server from the k8s session this playbook was handed, same pattern as host.py's gather_proxmox_evidence.
        async with streamablehttp_client(PROXMOX_MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as proxmox_session:
                await proxmox_session.initialize()
                result_text = await call_tool_text(proxmox_session, "list_nodes", {})
        count = len(json.loads(result_text))
        evidence = (
            "--- Proxmox cluster nodes (list_nodes) ---\n"
            f"Exact count (computed from the full list, not estimated): {count}\n"
            "Each entry below is a bare-metal Proxmox VE host running this homelab's "
            "hypervisor - these ARE the physical machines, not Kubernetes nodes or VMs.\n"
            f"{result_text}\n"
        )
    else:
        tool_name, tool_args = K8S_RESOURCES[resource]
        # max_chars=None - count rows from the full response, never a truncated one.
        result_text = await call_tool_text(session, tool_name, tool_args, max_chars=None)
        count = _count_table_rows(result_text)
        evidence = (
            f"--- {resource} ({tool_name}) ---\n"
            f"Exact count (computed from the full list, not estimated): {count}\n"
            f"{_sample(result_text)}\n"
        )

    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
