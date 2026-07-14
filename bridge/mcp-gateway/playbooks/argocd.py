"""Deterministic evidence-gathering for Argo CD application health alerts
(ArgoCDAppDegraded, ArgoCDAppNotSynced). These carry `name` (the Application
CR's own name) + `exported_namespace`/`dest_namespace` (what it deploys to,
not where the Application object lives - that's always `argocd`) - a
different identifying shape from pod/host/pvc/workload alerts. The single
best evidence source is the Application CR itself: its `status` already
carries sync/health state, the operation history, and a per-managed-resource
breakdown, which is exactly what a human would open the Argo CD UI to see."""
from common import call_tool_text, chat_completion, load_prompt

NAME = "argocd"


def extract_target(payload):
    for a in payload.get("alerts") or []:
        labels = a.get("labels", {})
        name = labels.get("name")
        if name and labels.get("job") == "argocd-application-controller-metrics":
            return {"name": name}
    return None


async def investigate(session, alert_text, target):
    name = target["name"]

    app_status = await call_tool_text(
        session, "resources_get", {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "name": name, "namespace": "argocd"}
    )
    events = await call_tool_text(
        session, "events_list", {"namespace": "argocd", "fieldSelector": f"involvedObject.name={name}"}
    )

    evidence = (
        f"--- Application status (resources_get Application {name}) ---\n{app_status}\n\n"
        f"--- events for this Application (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
