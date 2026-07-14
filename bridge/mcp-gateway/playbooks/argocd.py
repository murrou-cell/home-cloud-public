"""Deterministic evidence-gathering for Argo CD app alerts; Application CRs' namespace is resolved live (resolve_app_namespace), never assumed, even though this cluster currently puts them all in "argocd"."""
from common import call_tool_text, chat_completion, load_prompt, truncate_keeping_status

NAME = "argocd"
# A self-managing or resource-heavy Application (e.g. argocd's own, ~33KB) can exceed llama.cpp's
# 8192-token context entirely (confirmed: HTTP 400 from the server) if handed over unbounded -
# fetch the full object, then keep just this much, prioritizing .status (see truncate_keeping_status).
APP_STATUS_MAX_CHARS = 8000


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

    # max_chars=None on the fetch itself - even a typical Application is 8-11KB (well over the
    # default 2000-char cap), and the truncated-off tail is exactly where .status.sync/.status.health
    # live; truncate_keeping_status() below bounds what actually reaches the model instead.
    app_status = await call_tool_text(
        session,
        "resources_get",
        {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "name": name, "namespace": namespace},
        max_chars=None,
    )
    events = await call_tool_text(
        session, "events_list", {"namespace": namespace, "fieldSelector": f"involvedObject.name={name}"}
    )

    evidence = (
        f"--- Application status (resources_get Application {name}) ---\n"
        f"{truncate_keeping_status(app_status, APP_STATUS_MAX_CHARS)}\n\n"
        f"--- events for this Application (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
