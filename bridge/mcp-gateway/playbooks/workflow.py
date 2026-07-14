"""Direct /ask-only intent for a specific Argo Workflow custom resource's completion status and conditions.

This is a /ask-only playbook: it is wired solely into gateway.py's build_target() and must
NOT be added to playbooks/__init__.py's PLAYBOOKS list. Like argocd.py, it reads the Workflow
CR via the generic resources_get / resources_list / events_list tools; the Workflow's namespace
is resolved live (resolve_workflow_namespace) rather than assumed.
"""
from common import call_tool_text, chat_completion, load_prompt

NAME = "workflow"


def extract_target(payload):
    """/ask path passes a parsed question; expect the Workflow name (and optionally namespace)."""
    name = payload.get("name")
    if not name:
        return None
    target = {"name": name}
    namespace = payload.get("namespace")
    if namespace:
        target["namespace"] = namespace
    return target


async def resolve_workflow_namespace(session, name):
    """Lists Workflows across every namespace and reads the real NAMESPACE column for the matching
    name, instead of assuming one."""
    listing = await call_tool_text(
        session, "resources_list", {"apiVersion": "argoproj.io/v1alpha1", "kind": "Workflow"}, max_chars=None
    )
    for line in listing.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[3] == name:
            return parts[0]
    return None


async def investigate(session, alert_text, target):
    name = target["name"]

    namespace = target.get("namespace") or await resolve_workflow_namespace(session, name)
    if not namespace:
        return f"No Argo Workflow named '{name}' found in the cluster."

    # max_chars=None - even a simple single-step Workflow is ~7KB, well over the default 2000-char cap.
    wf_status = await call_tool_text(
        session,
        "resources_get",
        {"apiVersion": "argoproj.io/v1alpha1", "kind": "Workflow", "name": name, "namespace": namespace},
        max_chars=None,
    )
    events = await call_tool_text(
        session, "events_list", {"namespace": namespace, "fieldSelector": f"involvedObject.name={name}"}
    )

    evidence = (
        f"--- Workflow status (resources_get Workflow {name} in {namespace}) ---\n{wf_status}\n\n"
        f"--- events for this Workflow (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
