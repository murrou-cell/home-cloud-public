"""Deterministic evidence-gathering for workload-controller alerts - their `pod` label is always kube-state-metrics's exporter pod, not the real target (see pod.py); also resolves the controller's pod selector to list real pods, and fuzzy-matches a misspelled /ask-provided name via difflib."""
import difflib
import re

from common import MAX_TOOL_RESULT_CHARS, call_tool_text, chat_completion, load_prompt, truncate_keeping_status

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
    """resources_list renders a table; find the closest real name among rows matching this namespace+kind."""
    candidates = []
    for line in listing_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[0] == namespace and parts[2] == kind:
            candidates.append(parts[3])
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


def extract_label_selector(resource_text):
    """Extracts matchLabels as a selector string (None if absent, e.g. some PDBs/Jobs); the indentation backreference stops at the block's real end, since a StatefulSet's serviceName sits right after at a shallower indent and would otherwise be swallowed as another label."""
    match = re.search(
        r"matchLabels:\n(([ \t]+)[\w./-]+:[ \t]*[^\n]+\n?(?:\2[\w./-]+:[ \t]*[^\n]+\n?)*)",
        resource_text,
    )
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

    # max_chars=None: matchLabels can sit past the default cap for a bulkier controller; truncate only the model's copy, keeping status.
    controller_status = await call_tool_text(
        session, "resources_get", {"apiVersion": api_version, "kind": kind, "name": name, "namespace": namespace}, max_chars=None
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
                session, "resources_get", {"apiVersion": api_version, "kind": kind, "name": name, "namespace": namespace}, max_chars=None
            )

    events = await call_tool_text(
        session, "events_list", {"namespace": namespace, "fieldSelector": f"involvedObject.name={name}"}
    )

    pods_section = ""
    label_selector = extract_label_selector(controller_status)
    if label_selector:
        # A table listing has no "status:" marker for truncate_keeping_status to prioritize, so just
        # raise the flat cap instead - a listing across several pods was observed at ~9.8KB.
        pods = await call_tool_text(
            session,
            "resources_list",
            {"apiVersion": "v1", "kind": "Pod", "namespace": namespace, "labelSelector": label_selector},
            max_chars=MAX_TOOL_RESULT_CHARS * 3,
        )
        pods_section = f"--- pods under this {kind} (resources_list Pod, labelSelector={label_selector}) ---\n{pods}\n\n"

    evidence = (
        f"{resolved_note}"
        f"--- {kind} status (resources_get {kind} {name}) ---\n{truncate_keeping_status(controller_status, MAX_TOOL_RESULT_CHARS)}\n\n"
        f"{pods_section}"
        f"--- events for this {kind} (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
