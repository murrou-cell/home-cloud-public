"""Deterministic evidence-gathering for PVC alerts; namespace-less /ask questions are resolved cluster-wide via common.resolve_resource_namespace instead of letting the classifier invent one."""
import re

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from common import (
    GRAFANA_MCP_URL,
    call_tool_text,
    chat_completion,
    load_prompt,
    query_prometheus_scalar,
    resolve_resource_namespace,
)

NAME = "pvc"


async def gather_grafana_evidence(namespace, pvc_name):
    """Surfaces the Grafana dashboard/panel a human would check for disk usage, if one exists; unreachable/no-match is normal, never fatal."""
    try:
        async with streamablehttp_client(GRAFANA_MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # Grafana's dashboard search doesn't fuzzy-match multi-word queries well - "persistent volume" alone finds the real dashboard.
                search_result = await call_tool_text(
                    session, "search_dashboards", {"query": "persistent volume"}
                )
                return f"--- Grafana dashboards a human would check for disk usage (search_dashboards) ---\n{search_result}\n\n"
    except Exception:  # noqa: BLE001 - Grafana context is optional, never fatal to the diagnosis
        return ""


def extract_target(payload):
    for a in payload.get("alerts") or []:
        labels = a.get("labels", {})
        namespace = labels.get("namespace")
        pvc = labels.get("persistentvolumeclaim")
        if namespace and pvc:
            return {"namespace": namespace, "pvc": pvc}
    return None


def extract_volume_name(pvc_status_text):
    match = re.search(r"volumeName:\s*(\S+)", pvc_status_text)
    return match.group(1) if match else None


async def investigate(session, alert_text, target):
    namespace = target.get("namespace")
    pvc_name = target["pvc"]
    resolved_note = ""

    if not namespace:
        cluster_pvcs_text = await call_tool_text(
            session, "resources_list", {"apiVersion": "v1", "kind": "PersistentVolumeClaim"}, max_chars=None
        )
        match = resolve_resource_namespace(pvc_name, cluster_pvcs_text)
        if not match:
            return f"No PVC matching '{pvc_name}' found anywhere in the cluster."
        status, data = match
        if status == "ambiguous":
            options = ", ".join(f"'{n}' (namespace '{ns}')" for n, ns in data.items())
            return (
                f"'{pvc_name}' matches more than one different PVC, not replicas of the "
                f"same one: {options}. Please ask again naming the specific PVC or its namespace."
            )
        namespace, resolved_name = data
        resolved_note = (
            f"(Note: no namespace was given - resolved '{pvc_name}' to PVC "
            f"'{resolved_name}' in namespace '{namespace}'.)\n"
        )
        pvc_name = resolved_name

    pvc_status = await call_tool_text(
        session, "resources_get", {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "name": pvc_name, "namespace": namespace}
    )
    volume_name = extract_volume_name(pvc_status)
    pv_status = ""
    if volume_name:
        pv_status = await call_tool_text(
            session, "resources_get", {"apiVersion": "v1", "kind": "PersistentVolume", "name": volume_name}
        )
    events = await call_tool_text(
        session, "events_list", {"namespace": namespace, "fieldSelector": f"involvedObject.name={pvc_name}"}
    )

    # PVC/PV objects only expose requested capacity, never real usage; query Prometheus directly instead of inviting a fabricated fullness claim.
    usage_percent = query_prometheus_scalar(
        f'kubelet_volume_stats_used_bytes{{namespace="{namespace}",persistentvolumeclaim="{pvc_name}"}} '
        f'/ kubelet_volume_stats_capacity_bytes{{namespace="{namespace}",persistentvolumeclaim="{pvc_name}"}} * 100'
    )
    usage_evidence = (
        f"{usage_percent:.1f}% used" if usage_percent is not None else "no usage metrics available for this PVC"
    )
    grafana_evidence = await gather_grafana_evidence(namespace, pvc_name)

    evidence = (
        f"{resolved_note}"
        f"--- PVC status (resources_get PersistentVolumeClaim {pvc_name}) ---\n{pvc_status}\n\n"
        + (f"--- bound PersistentVolume ({volume_name}) ---\n{pv_status}\n\n" if volume_name else "")
        + f"--- real disk usage (Prometheus kubelet_volume_stats) ---\n{usage_evidence}\n\n"
        + grafana_evidence
        + f"--- events for this PVC (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
