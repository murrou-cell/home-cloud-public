"""Deterministic evidence-gathering for workload-controller alerts
(KubeDeploymentReplicasMismatch, KubeDaemonSetRolloutStuck/MisScheduled,
KubeStatefulSetReplicasMismatch, KubeJobFailed, KubePdbNotEnoughHealthyPods).
These are the alerts pod.py's old label-shape-only match would have
misrouted (see pod.py's docstring) - they carry namespace + a
controller-identifying label, but the `pod` label present alongside it is
always kube-state-metrics's own exporter pod, not the real target, because
none of these object kinds have a per-pod dimension in their own metric for
honor_labels to override it with.

A controller's own status (replica counts, conditions) rarely explains *why*
it's unhealthy - the harness also resolves the controller's pod selector and
lists the actual pods under it, since a stuck rollout is almost always a
specific pod failing to become ready (image pull, crash loop, resource
limits), same reasoning as pod.py fetching logs instead of just pod status.

Also reused by gateway.py's /ask endpoint (question -> intent-classified
target, not an alert) - a user's question or the classifier's own extraction
can name a controller slightly wrong (observed live: "argocd-repo-server"
misspelled as "argo-cd-repo-server"), so a not-found lookup falls back to
fuzzy-matching the closest real name in the namespace, same idea as
pod.py's resolve_pod_name but via difflib since the mismatch here isn't a
predictable hash-suffix pattern."""
import difflib
import re

from common import call_tool_text, chat_completion, load_prompt

NAME = "workload"

# label key -> (apiVersion, kind)
CONTROLLER_KINDS = {
    "deployment": ("apps/v1", "Deployment"),
    "daemonset": ("apps/v1", "DaemonSet"),
    "statefulset": ("apps/v1", "StatefulSet"),
    "job_name": ("batch/v1", "Job"),
    "poddisruptionbudget": ("policy/v1", "PodDisruptionBudget"),
}


def extract_target(payload):
    for a in payload.get("alerts") or []:
        labels = a.get("labels", {})
        namespace = labels.get("namespace")
        if not namespace:
            continue
        for label_key, (api_version, kind) in CONTROLLER_KINDS.items():
            name = labels.get(label_key)
            if name:
                return {"namespace": namespace, "name": name, "apiVersion": api_version, "kind": kind}
    return None


def resolve_controller_name(name, namespace, kind, listing_text):
    """resources_list renders a NAMESPACE/APIVERSION/KIND/NAME/... table -
    find the closest real name among rows matching this namespace+kind."""
    candidates = []
    for line in listing_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[0] == namespace and parts[2] == kind:
            candidates.append(parts[3])
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


def extract_label_selector(resource_text):
    """PodDisruptionBudgets and Jobs don't always carry matchLabels the same
    way Deployments/DaemonSets/StatefulSets do - callers treat None as
    "couldn't derive a selector, evidence is controller status + events only"."""
    match = re.search(r"matchLabels:\n((?:[ \t]+[\w./-]+:[ \t]*[^\n]+\n?)+)", resource_text)
    if not match:
        return None
    pairs = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        key, _, value = line.partition(":")
        pairs.append(f"{key.strip()}={value.strip()}")
    return ",".join(pairs) if pairs else None


async def investigate(session, alert_text, target):
    namespace = target["namespace"]
    name = target["name"]
    api_version = target["apiVersion"]
    kind = target["kind"]

    controller_status = await call_tool_text(
        session, "resources_get", {"apiVersion": api_version, "kind": kind, "name": name, "namespace": namespace}
    )
    resolved_note = ""
    if "not found" in controller_status.lower() or controller_status.lower().startswith("tool error"):
        listing = await call_tool_text(
            session, "resources_list", {"apiVersion": api_version, "kind": kind, "namespace": namespace}, max_chars=None
        )
        candidate = resolve_controller_name(name, namespace, kind, listing)
        if candidate:
            resolved_note = (
                f"(Note: '{name}' not found - resolved to the closest matching "
                f"{kind} in this namespace, '{candidate}'.)\n"
            )
            name = candidate
            controller_status = await call_tool_text(
                session, "resources_get", {"apiVersion": api_version, "kind": kind, "name": name, "namespace": namespace}
            )

    events = await call_tool_text(
        session, "events_list", {"namespace": namespace, "fieldSelector": f"involvedObject.name={name}"}
    )

    pods_section = ""
    label_selector = extract_label_selector(controller_status)
    if label_selector:
        pods = await call_tool_text(
            session, "resources_list", {"apiVersion": "v1", "kind": "Pod", "namespace": namespace, "labelSelector": label_selector}
        )
        pods_section = f"--- pods under this {kind} (resources_list Pod, labelSelector={label_selector}) ---\n{pods}\n\n"

    evidence = (
        f"{resolved_note}"
        f"--- {kind} status (resources_get {kind} {name}) ---\n{controller_status}\n\n"
        f"{pods_section}"
        f"--- events for this {kind} (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
