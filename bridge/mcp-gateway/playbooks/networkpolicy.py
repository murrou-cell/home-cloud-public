"""Direct /ask-only intent for a specific NetworkPolicy's rules (e.g. "does the mcp-gateway NetworkPolicy allow egress to X?"). Reads the real NetworkPolicy object via the generic resources_get tool - the Kubernetes MCP session is the default, so no MCP_URL constant is needed."""
from common import call_tool_text, chat_completion, load_prompt

NAME = "networkpolicy"


async def investigate(session, alert_text, target):
    name = target["name"]
    namespace = target.get("namespace")

    if not namespace:
        return "A NetworkPolicy question needs a namespace - NetworkPolicies are namespaced and the same name can exist in several namespaces."

    # max_chars=None - a policy with several egress rules easily exceeds the default 2000-char
    # truncation, cutting off exactly the rule the question is about (observed: truncated right
    # before the last egress rule, causing a confidently wrong "not allowed" answer).
    policy = await call_tool_text(
        session,
        "resources_get",
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "name": name, "namespace": namespace},
        max_chars=None,
    )

    evidence = (
        f"--- NetworkPolicy {namespace}/{name} (resources_get NetworkPolicy) ---\n{policy}\n"
    )
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
