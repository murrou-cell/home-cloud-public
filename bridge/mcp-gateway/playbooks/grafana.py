"""Direct /ask-only intent: what dashboard covers a topic - deliberately just search_dashboards, not a real metric query, since turning free text into PromQL is exactly the open-ended task this pipeline avoids handing the model."""
import json

from common import GRAFANA_MCP_URL, call_tool_text, chat_completion, load_prompt

NAME = "grafana"
MCP_URL = GRAFANA_MCP_URL


def _has_results(search_text):
    try:
        return bool(json.loads(search_text).get("dashboards"))
    except (json.JSONDecodeError, AttributeError):
        return False


async def investigate(session, alert_text, target):
    query = target["query"]
    search_result = await call_tool_text(session, "search_dashboards", {"query": query})
    # Grafana's search matches the whole query as one substring, not per-word AND; fall back to individual words, longest first.
    if not _has_results(search_result):
        words = sorted({w for w in query.split() if len(w) > 2 and w != query}, key=len, reverse=True)
        for word in words:
            fallback = await call_tool_text(session, "search_dashboards", {"query": word})
            if _has_results(fallback):
                search_result = fallback
                query = word
                break
    evidence = f"--- Grafana dashboards matching '{query}' (search_dashboards) ---\n{search_result}\n"
    prompt = load_prompt("diagnosis.txt", alert_text=alert_text, evidence=evidence)
    message = chat_completion([{"role": "user", "content": prompt}])
    return message.get("content") or "(model returned no diagnosis text)"
