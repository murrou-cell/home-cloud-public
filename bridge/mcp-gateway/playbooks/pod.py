"""Deterministic evidence-gathering for pod-shaped alerts (KubePodCrashLooping,
KubePodNotReady). The harness itself fetches pod status, logs, and events in
a fixed sequence rather than letting the model choose tools - Qwen2.5-3B
reliably summarizes evidence handed to it, but unreliably plans a multi-step
investigation itself: live testing showed it hallucinate a plausible-but-wrong
ConfigMap name after a "not found" on an invented one, then narrate
resources_create/pods_update calls that were never in its actual tool list at
all. That's a reasoning-capability ceiling, not something more prompting
fixes - so the harness plans instead of the model.

Alertname is whitelisted rather than just checking "namespace and pod are
both present": confirmed live against real alert history that
KubeDeploymentReplicasMismatch/KubeDaemonSetRolloutStuck/
KubeStatefulSetReplicasMismatch/KubeJobFailed/KubePdbNotEnoughHealthyPods all
carry namespace+pod too, but `pod` there is kube-state-metrics's own exporter
pod leaking through (those alerts' underlying metrics have no per-pod
dimension, so there's nothing for honor_labels to override it with) - not
the actual target. Matching on label-shape alone would have silently
investigated the always-healthy kube-state-metrics pod instead of the
broken Deployment/DaemonSet/StatefulSet/Job/PDB (see workload.py, which
owns those alertnames instead)."""
import difflib
import re

from common import call_tool_text, chat_completion, load_prompt

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
    """A pod named in an alert may already be gone by investigation time
    (CrashLoopBackOff pods get recreated with a new ReplicaSet hash), or
    gateway.py's /ask endpoint may pass a short partial name a user typed
    (e.g. "loki" instead of "loki-stack-0") - either way, find the closest
    real pod in the namespace sharing the queried name's stem.

    Tiebreak is difflib similarity to the queried name, not "prefer the
    longest match": confirmed live that "prefer longest" picked
    loki-stack-alloy-5wr2q (an unrelated sidecar pod that also starts with
    "loki") over the much closer loki-stack-0, when the query was the short
    partial name "loki" - difflib correctly ranks loki-stack-0 higher
    (0.5 vs 0.31 similarity) since it's comparing overall closeness to what
    was actually asked for, not just which candidate happens to be longer.

    Candidates come strictly from the table's NAME column (index 3 -
    NAMESPACE/APIVERSION/KIND/NAME/...), not a blanket regex over the whole
    row: confirmed live that scanning the entire text picked up "loki-stack"
    as a false candidate - not a real pod, a label *value* from
    app.kubernetes.io/instance=loki-stack in the LABELS column - which then
    out-scored the real loki-stack-0 on pure string similarity to "loki"."""
    stem = re.sub(r"(-[a-z0-9]{5,10}){1,2}$", "", pod_name)
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
    namespace = target["namespace"]
    pod_name = target["pod"]

    pod_status = await call_tool_text(session, "pods_get", {"name": pod_name, "namespace": namespace})
    resolved_note = ""
    if "not found" in pod_status.lower() or pod_status.lower().startswith("tool error"):
        pods_list_text = await call_tool_text(
            session, "pods_list_in_namespace", {"namespace": namespace}, max_chars=None
        )
        candidate = resolve_pod_name(pod_name, pods_list_text)
        if candidate:
            resolved_note = (
                f"(Note: alert named '{pod_name}', which no longer exists - resolved to "
                f"current pod '{candidate}' in the same namespace with a matching name stem.)\n"
            )
            pod_name = candidate
            pod_status = await call_tool_text(session, "pods_get", {"name": pod_name, "namespace": namespace})

    logs = await call_tool_text(session, "pods_log", {"name": pod_name, "namespace": namespace, "tail": 200})
    events = await call_tool_text(
        session, "events_list", {"namespace": namespace, "fieldSelector": f"involvedObject.name={pod_name}"}
    )

    evidence = (
        f"{resolved_note}"
        f"--- pod status (pods_get {pod_name}) ---\n{pod_status}\n\n"
        f"--- recent logs (pods_log {pod_name}, tail=200) ---\n{logs}\n\n"
        f"--- events for this pod (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
