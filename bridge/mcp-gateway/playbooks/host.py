"""Deterministic evidence-gathering for node/host alerts; bare-metal Proxmox hosts share this alert shape but aren't visible to kubernetes-mcp-server (see gather_proxmox_evidence), so gated on job=node-exporter/kubelet minus persistentvolumeclaim (pvc.py's field), not pod/namespace absence - real in-cluster node alerts also carry a node-exporter DaemonSet pod label."""
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from common import PROXMOX_MCP_URL, call_tool_text, chat_completion, extract_node_memory_summary, extract_proxmox_memory_summary, load_prompt, summarize_node_pod_stats

NAME = "host"


async def gather_proxmox_evidence(instance):
    """Resolves the alerting instance's IP to a Proxmox node and pulls its status + task log; unreachable/unmatched is normal here, never fatal."""
    ip = instance.split(":")[0]
    try:
        async with streamablehttp_client(PROXMOX_MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                nodes_text = await call_tool_text(session, "list_nodes", {})
                node_name = None
                for entry in json.loads(nodes_text):
                    if entry.get("ip") == ip:
                        node_name = entry.get("name")
                        break
                if not node_name:
                    return None
                status = await call_tool_text(session, "get_node_status", {"node": node_name})
                tasks = await call_tool_text(session, "list_recent_tasks", {"node": node_name, "limit": 10})
                memory_summary = extract_proxmox_memory_summary(status)
                memory_section = f"--- real memory/swap/disk figures (computed from get_node_status) ---\n{memory_summary}\n\n" if memory_summary else ""
                return (
                    f"--- Proxmox node status (get_node_status {node_name}) ---\n{status}\n\n"
                    f"{memory_section}"
                    f"--- recent Proxmox tasks on {node_name} (list_recent_tasks) ---\n{tasks}\n"
                )
    except Exception:  # noqa: BLE001 - Proxmox context is optional, never fatal to the diagnosis
        return None


def extract_target(payload):
    for a in payload.get("alerts") or []:
        labels = a.get("labels", {})
        instance = labels.get("instance")
        if (
            instance
            and labels.get("job") in ("node-exporter", "kubelet")
            and not labels.get("persistentvolumeclaim")
        ):
            return {
                "instance": instance,
                "node_label": labels.get("node"),
                "cluster_label": labels.get("cluster"),
            }
    return None


def resolve_node_name(ip, nodes_table_text):
    """resources_list renders Nodes as a table with NAME/INTERNAL-IP as fixed columns 3/8, ahead of the free-text OS-IMAGE column."""
    for line in nodes_table_text.splitlines():
        parts = line.split()
        if len(parts) >= 8 and parts[1] == "Node" and parts[7] == ip:
            return parts[2]
    return None


async def diagnose(alert_text, evidence):
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"


async def investigate(session, alert_text, target):
    # instance is alert-only; /ask sets only node_label since there's no scrape-target instance for a directly-named node.
    instance = target.get("instance")
    node_name = target.get("node_label")

    if not node_name and target.get("cluster_label") == "proxmox-hosts":
        proxmox_evidence = await gather_proxmox_evidence(instance) if instance else None
        evidence = (
            f"--- target ---\n"
            f"instance {instance} is a bare-metal Proxmox host reached via a static "
            f"node-exporter scrape target (kube-prometheus-stack additionalScrapeConfigs, "
            f"cluster=proxmox-hosts), not a Kubernetes node. kubernetes-mcp-server only has "
            f"API access to the k3s cluster, so real evidence for this host comes from "
            f"proxmox-mcp instead.\n\n"
            + (proxmox_evidence or "proxmox-mcp had no data for this instance - the alert's own description is all there is.\n")
        )
        return await diagnose(alert_text, evidence)

    nodes_table = None
    if not node_name and instance:
        ip = instance.split(":")[0]
        # max_chars=None - a handful of nodes never approaches the context limit, so no tradeoff.
        nodes_table = await call_tool_text(session, "resources_list", {"apiVersion": "v1", "kind": "Node"}, max_chars=None)
        node_name = resolve_node_name(ip, nodes_table)

    if not node_name:
        evidence = (
            f"--- target ---\ninstance {instance} does not match any Kubernetes node's "
            f"InternalIP in this cluster - could not resolve which node the alert refers to.\n"
            f"--- resources_list Node ---\n{nodes_table}\n"
        )
        return await diagnose(alert_text, evidence)

    # max_chars=None - a real Node object is well over the default 2000-char cap (observed
    # ~12.8KB), which was cutting off conditions/allocatable/taints before the model ever saw them.
    node_status = await call_tool_text(
        session, "resources_get", {"apiVersion": "v1", "kind": "Node", "name": node_name}, max_chars=None
    )
    # Real KiB values invite the same unit-conversion hallucination as per-pod stats; compute GiB deterministically instead.
    memory_summary = extract_node_memory_summary(node_status)
    node_top = await call_tool_text(session, "nodes_top", {"name": node_name})
    # max_chars=None - the default 2000-char cap dropped the whole per-pod array, causing a fabricated byte count; parse deterministically instead.
    pod_stats_raw = await call_tool_text(session, "nodes_stats_summary", {"name": node_name}, max_chars=None)
    pod_stats = summarize_node_pod_stats(pod_stats_raw)
    events = await call_tool_text(
        session, "events_list", {"fieldSelector": f"involvedObject.name={node_name}"}
    )

    memory_section = f"--- real memory figures (computed from resources_get) ---\n{memory_summary}\n\n" if memory_summary else ""
    evidence = (
        f"--- node status (resources_get Node {node_name}) ---\n{node_status}\n\n"
        f"{memory_section}"
        f"--- node resource usage (nodes_top {node_name}) ---\n{node_top}\n\n"
        f"--- per-pod stats on this node (nodes_stats_summary {node_name}) ---\n{pod_stats}\n\n"
        f"--- events for this node (events_list) ---\n{events}\n"
    )
    return await diagnose(alert_text, evidence)
