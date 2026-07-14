"""Deterministic evidence-gathering for Argo Workflow failure alerts; the Workflow CR's namespace is resolved live (resolve_workflow_namespace), never assumed. The alert text often only says "unknown error", so the real evidence is the Workflow CR's status (phase/message, per-node failures) plus its events - which the open-ended fallback ran out of context budget gathering."""
from common import call_tool_text, chat_completion, load_prompt

NAME = "argo_workflow"


def extract_target(payload):
    for a in payload.get("alerts") or []:
        labels = a.get("labels", {})
        if labels.get("alertname") == "ArgoWorkflowFailed":
            name = labels.get("name") or labels.get("workflow")
            if name:
                target = {"name": name}
                ns = labels.get("namespace")
                if ns:
                    target["namespace"] = ns
                return target
    return None


async def resolve_workflow_namespace(session, name):
    """Lists Workflows across every namespace and reads the real NAMESPACE column for the matching name, instead of assuming one."""
    listing = await call_tool_text(
        session, "resources_list", {"apiVersion": "argoproj.io/v1alpha1", "kind": "Workflow"}, max_chars=None
    )
    for line in listing.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == name:
            return parts[0]
    return None


async def investigate(session, alert_text, target):
    name = target["name"]

    namespace = target.get("namespace") or await resolve_workflow_namespace(session, name)
    if not namespace:
        return f"No Argo Workflow named '{name}' found in the cluster."

    # max_chars=None - even a simple single-step Workflow is ~7KB, well over the default 2000-char cap.
    workflow_status = await call_tool_text(
        session,
        "resources_get",
        {"apiVersion": "argoproj.io/v1alpha1", "kind": "Workflow", "name": name, "namespace": namespace},
        max_chars=None,
    )
    events = await call_tool_text(
        session, "events_list", {"namespace": namespace, "fieldSelector": f"involvedObject.name={name}"}
    )

    evidence = (
        f"--- Workflow status (resources_get Workflow {name}) ---\n{workflow_status}\n\n"
        f"--- events for this Workflow (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
