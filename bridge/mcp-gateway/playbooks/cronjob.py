"""Direct /ask-only intent for a specific CronJob's health and last successful run; the workload intent covers Deployment/DaemonSet/StatefulSet/Job/PDB but not CronJob, whose scheduling status (lastScheduleTime/lastSuccessfulTime/active) and spawned Jobs need their own evidence-gathering. Namespace is optional and resolved live when omitted, like pvc.py."""
from common import call_tool_text, chat_completion, load_prompt

NAME = "cronjob"


async def resolve_cronjob_namespace(session, name):
    """Lists CronJobs across every namespace and reads the real NAMESPACE column for the matching name, instead of assuming one."""
    listing = await call_tool_text(
        session, "resources_list", {"apiVersion": "batch/v1", "kind": "CronJob"}, max_chars=None
    )
    for line in listing.splitlines()[1:]:
        parts = line.split()
        # resources_list rows are "NAMESPACE NAME ..." for namespaced kinds.
        if len(parts) >= 2 and parts[1] == name:
            return parts[0]
    return None


async def investigate(session, alert_text, target):
    name = target["cronjob"]
    namespace = target.get("namespace")

    if not namespace:
        namespace = await resolve_cronjob_namespace(session, name)
        if not namespace:
            return f"No CronJob named '{name}' found in the cluster."

    cronjob_status = await call_tool_text(
        session, "resources_get", {"apiVersion": "batch/v1", "kind": "CronJob", "name": name, "namespace": namespace}
    )
    # The Jobs a CronJob spawns carry the actual success/failure of each run; the CronJob's own
    # status only records lastScheduleTime / lastSuccessfulTime and currently-active Job refs.
    jobs = await call_tool_text(
        session, "resources_list", {"apiVersion": "batch/v1", "kind": "Job", "namespace": namespace}, max_chars=None
    )
    events = await call_tool_text(
        session, "events_list", {"namespace": namespace, "fieldSelector": f"involvedObject.name={name}"}
    )

    evidence = (
        f"--- CronJob status (resources_get CronJob {name} in {namespace}) ---\n{cronjob_status}\n\n"
        f"--- Jobs in namespace {namespace} (resources_list Job) - identify those owned by this CronJob and their completion times/status ---\n{jobs}\n\n"
        f"--- events for this CronJob (events_list) ---\n{events}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
