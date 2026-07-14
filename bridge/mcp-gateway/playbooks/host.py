"""Deterministic evidence-gathering for node/host alerts - node-exporter
(NodeMemoryHighUtilization etc) and kubelet (KubeletInstanceUnreachable etc)
jobs. Two different things share the node-exporter shape on this cluster: a
real k3s Node, or one of the bare-metal Proxmox hosts scraped by the static
`cluster: proxmox-hosts` target in kube-prometheus-stack's
additionalScrapeConfigs (see argocd/apps/kube-prometheus-stack/values.yaml).
kubernetes-mcp-server has no API access to the latter - confirmed live
(instance <YOUR_PROXMOX_HOST_IP> is pve2, not a node in `kubectl get nodes`) - so
there's nothing to gather for those beyond saying so plainly instead of the
model inventing pod-level advice.

Gate is job-based, not "no pod/namespace" - confirmed live against real
alert history that a real in-cluster NodeMemoryHighUtilization/
NodeSystemSaturation/NodeMemoryMajorPagesFaults DOES carry namespace=monitoring
+ pod=<node-exporter DaemonSet pod> (same kube-state-metrics-style exporter
leak as workload.py's docstring describes, just via the node-exporter
DaemonSet pod's own identity instead) - excluding on pod/namespace presence
would have rejected every real in-cluster node alert and only matched the
bare-metal case, which happens to carry no k8s scrape metadata at all.
`persistentvolumeclaim` is excluded instead, since that's pvc.py's
distinctive, reliably-honored field (KubePersistentVolumeFillingUp is also
job=kubelet but about a PVC, not the node itself)."""
from common import call_tool_text, chat_completion, load_prompt

NAME = "host"


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
    """resources_list renders Nodes as a whitespace-column table - NAME and
    INTERNAL-IP are the 3rd/8th columns and always single tokens, ahead of
    the free-text OS-IMAGE column, so a fixed left-hand split is safe."""
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
    # instance is alert-only (node-exporter/kubelet labels always carry it);
    # gateway.py's /ask endpoint builds a target from a chat question with
    # only node_label set, since a user names a node directly and there's no
    # scrape-target instance to speak of - both must work without crashing.
    instance = target.get("instance")
    node_name = target.get("node_label")

    if not node_name and target.get("cluster_label") == "proxmox-hosts":
        evidence = (
            f"--- target ---\n"
            f"instance {instance} is a bare-metal Proxmox host reached via a static "
            f"node-exporter scrape target (kube-prometheus-stack additionalScrapeConfigs, "
            f"cluster=proxmox-hosts), not a Kubernetes node. The kubernetes-mcp-server this "
            f"harness uses only has API access to the k3s cluster, so no further evidence "
            f"can be gathered here - the alert's own description is all there is.\n"
        )
        return await diagnose(alert_text, evidence)

    nodes_table = None
    if not node_name and instance:
        ip = instance.split(":")[0]
        # max_chars=None: same truncation bug confirmed live in pod.py/workload.py's
        # resolution helpers - a handful of nodes in this homelab never approaches
        # llama.cpp's context limit even untruncated, so there's no tradeoff here.
        nodes_table = await call_tool_text(session, "resources_list", {"apiVersion": "v1", "kind": "Node"}, max_chars=None)
        node_name = resolve_node_name(ip, nodes_table)

    if not node_name:
        evidence = (
            f"--- target ---\ninstance {instance} does not match any Kubernetes node's "
            f"InternalIP in this cluster - could not resolve which node the alert refers to.\n"
            f"--- resources_list Node ---\n{nodes_table}\n"
        )
        return await diagnose(alert_text, evidence)

    node_status = await call_tool_text(
        session, "resources_get", {"apiVersion": "v1", "kind": "Node", "name": node_name}
    )
    node_top = await call_tool_text(session, "nodes_top", {"name": node_name})
    pod_stats = await call_tool_text(session, "nodes_stats_summary", {"name": node_name})
    events = await call_tool_text(
        session, "events_list", {"fieldSelector": f"involvedObject.name={node_name}"}
    )

    evidence = (
        f"--- node status (resources_get Node {node_name}) ---\n{node_status}\n\n"
        f"--- node resource usage (nodes_top {node_name}) ---\n{node_top}\n\n"
        f"--- per-pod stats on this node (nodes_stats_summary {node_name}) ---\n{pod_stats}\n\n"
        f"--- events for this node (events_list) ---\n{events}\n"
    )
    return await diagnose(alert_text, evidence)
