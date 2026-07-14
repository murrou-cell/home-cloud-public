"""Deterministic evidence-gathering for PVC-shaped alerts
(KubePersistentVolumeFillingUp etc). These carry `namespace` + the honored,
metric-native `persistentvolumeclaim` label - a different identifying shape
from pod/host/argocd/workload alerts (see host.py's docstring for why
`job=kubelet` alone isn't enough to tell this apart from a node alert)."""
import re

from common import call_tool_text, chat_completion, load_prompt

NAME = "pvc"


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
    namespace = target["namespace"]
    pvc_name = target["pvc"]

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

    evidence = (
        f"--- PVC status (resources_get PersistentVolumeClaim {pvc_name}) ---\n{pvc_status}\n\n"
        + (f"--- bound PersistentVolume ({volume_name}) ---\n{pv_status}\n\n" if volume_name else "")
        + f"--- events for this PVC (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
