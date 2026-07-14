"""Direct /ask-only intent for a specific Proxmox host's status (alerts reach the same evidence via host.py's bare-metal branch); "how many hosts" is list_resource.py instead."""
import json

from common import PROXMOX_MCP_URL, call_tool_text, chat_completion, extract_proxmox_memory_summary, load_prompt

NAME = "proxmox"
MCP_URL = PROXMOX_MCP_URL


async def investigate(session, alert_text, target):
    node = target["node"]
    # Resolve against the live cluster (list_nodes), not a hardcoded whitelist, so a renamed/added host needs no code change.
    nodes_text = await call_tool_text(session, "list_nodes", {})
    real_names = [n.get("name") for n in json.loads(nodes_text)]
    if node not in real_names:
        return f"'{node}' is not a Proxmox host in this cluster. Known hosts: {', '.join(real_names) or 'none reachable'}."
    status = await call_tool_text(session, "get_node_status", {"node": node})
    tasks = await call_tool_text(session, "list_recent_tasks", {"node": node, "limit": 10})
    memory_summary = extract_proxmox_memory_summary(status)
    memory_section = f"--- real memory/swap/disk figures (computed from get_node_status) ---\n{memory_summary}\n\n" if memory_summary else ""
    evidence = (
        f"--- Proxmox node status (get_node_status {node}) ---\n{status}\n\n"
        f"{memory_section}"
        f"--- recent Proxmox tasks on {node} (list_recent_tasks) ---\n{tasks}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
