"""Deterministic evidence-gathering for Argo CD app alerts; Application CRs' namespace is resolved live (resolve_app_namespace), never assumed, even though this cluster currently puts them all in "argocd"."""
from common import call_tool_text, chat_completion, load_prompt

NAME = "argocd"


def extract_target(payload):
    for a in payload.get("alerts") or []:
        labels = a.get("labels", {})
        name = labels.get("name")
        if name and labels.get("job") == "argocd-application-controller-metrics":
            return {"name": name}
    return None


async def resolve_app_namespace(session, name):
    """Lists Applications across every namespace and reads the real NAMESPACE column for the matching name, instead of assuming one."""
    listing = await call_tool_text(
        session, "resources_list", {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application"}, max_chars=None
    )
    for line in listing.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[3] == name:
            return parts[0]
    return None


async def investigate(session, alert_text, target):
    name = target["name"]

    namespace = await resolve_app_namespace(session, name)
    if not namespace:
        return f"No Argo CD Application named '{name}' found in the cluster."

    app_status = await call_tool_text(
        session, "resources_get", {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "name": name, "namespace": namespace}
    )
    events = await call_tool_text(
        session, "events_list", {"namespace": namespace, "fieldSelector": f"involvedObject.name={name}"}
    )

    evidence = (
        f"--- Application status (resources_get Application {name}) ---\n{app_status}\n\n"
        f"--- events for this Application (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
