"""Direct /ask-only intent: read the current value of a named Longhorn Setting.

Longhorn stores each of its settings as a Setting custom resource in the
longhorn-system namespace (settings.longhorn.io/<name>), whose .value field
holds the currently-effective value. This playbook fetches that one resource
deterministically and lets the model compare it against the documented default
- exactly the kind of single-object lookup the pipeline can do without guessing.
"""
from common import call_tool_text, chat_completion, load_prompt

NAME = "longhorn_setting"


async def investigate(session, alert_text, target):
    name = target["name"]
    setting = await call_tool_text(
        session,
        "resources_get",
        {
            "apiVersion": "longhorn.io/v1beta2",
            "kind": "Setting",
            "namespace": "longhorn-system",
            "name": name,
        },
    )
    evidence = (
        f"--- Longhorn Setting '{name}' "
        f"(resources_get settings.longhorn.io/{name} in longhorn-system) ---\n"
        f"{setting}\n"
        "Note: the Setting resource's .value field is the currently-effective value; "
        "an empty or absent .value means the setting is using its built-in default. "
        "This resource never exposes what that built-in default actually is - if asked whether "
        "the value matches the default, say the default isn't shown in this evidence rather than "
        "stating a specific default number.\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
