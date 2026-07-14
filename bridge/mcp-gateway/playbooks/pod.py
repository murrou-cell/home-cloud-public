"""Deterministic evidence-gathering for pod alerts (fixed status/logs/events sequence, not model-planned - it hallucinates when trusted to plan); alertname is whitelisted since some workload alerts carry a stale kube-state-metrics pod label too (see workload.py)."""
import difflib
import re

from common import (
    HASH_SUFFIX_RE,
    MAX_TOOL_RESULT_CHARS,
    call_tool_text,
    chat_completion,
    load_prompt,
    resolve_resource_namespace,
    truncate_keeping_status,
)

NAME = "pod"

ALERTNAMES = {"KubePodCrashLooping", "KubePodNotReady"}


def extract_target(payload):
    for a in payload.get("alerts") or []:
        labels = a.get("labels", {})
        namespace = labels.get("namespace")
        pod = labels.get("pod")
        if labels.get("alertname") in ALERTNAMES and namespace and pod:
            return {"namespace": namespace, "pod": pod}
    return None


def resolve_pod_name(pod_name, pods_list_text):
    """Finds the closest real pod in the namespace sharing the queried name's stem (e.g. after a CrashLoopBackOff recreate), via difflib tiebreak on the NAME column only."""
    stem = re.sub(HASH_SUFFIX_RE, "", pod_name)
    names = set()
    for line in pods_list_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            names.add(parts[3])
    candidates = {c for c in names if c.startswith(stem) and c != pod_name}
    if not candidates:
        return None
    return max(candidates, key=lambda c: difflib.SequenceMatcher(None, pod_name, c).ratio())


async def investigate(session, alert_text, target):
    namespace = target.get("namespace")
    pod_name = target["pod"]
    resolved_note = ""

    if not namespace:
        cluster_pods_text = await call_tool_text(session, "pods_list", {}, max_chars=None)
        match = resolve_resource_namespace(pod_name, cluster_pods_text)
        if not match:
            return f"No pod matching '{pod_name}' found anywhere in the cluster."
        status, data = match
        if status == "ambiguous":
            options = ", ".join(f"'{n}' (namespace '{ns}')" for n, ns in data.items())
            return (
                f"'{pod_name}' matches more than one different pod, not replicas of the "
                f"same one: {options}. Please ask again naming the specific pod or its namespace."
            )
        namespace, resolved_name = data
        resolved_note = (
            f"(Note: no namespace was given - resolved '{pod_name}' to pod "
            f"'{resolved_name}' in namespace '{namespace}'.)\n"
        )
        pod_name = resolved_name

    # max_chars=None: status can sit past the default cap; truncate_keeping_status keeps it instead of a flat prefix.
    pod_status = await call_tool_text(session, "pods_get", {"name": pod_name, "namespace": namespace}, max_chars=None)
    if "not found" in pod_status.lower() or pod_status.lower().startswith("tool error"):
        pods_list_text = await call_tool_text(
            session, "pods_list_in_namespace", {"namespace": namespace}, max_chars=None
        )
        candidate = resolve_pod_name(pod_name, pods_list_text)
        if candidate:
            resolved_note += (
                f"(Note: '{pod_name}' no longer exists - resolved to current pod "
                f"'{candidate}' in the same namespace with a matching name stem.)\n"
            )
            pod_name = candidate
            pod_status = await call_tool_text(session, "pods_get", {"name": pod_name, "namespace": namespace}, max_chars=None)

    logs = await call_tool_text(session, "pods_log", {"name": pod_name, "namespace": namespace, "tail": 200})
    events = await call_tool_text(
        session, "events_list", {"namespace": namespace, "fieldSelector": f"involvedObject.name={pod_name}"}
    )

    evidence = (
        f"{resolved_note}"
        f"--- pod status (pods_get {pod_name}) ---\n{truncate_keeping_status(pod_status, MAX_TOOL_RESULT_CHARS)}\n\n"
        f"--- recent logs (pods_log {pod_name}, tail=200) ---\n{logs}\n\n"
        f"--- events for this pod (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
