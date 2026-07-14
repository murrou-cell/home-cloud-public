"""Shared HTTP/MCP helpers and config used by gateway.py, investigate.py,
agentic.py, and the playbooks/ modules."""
import json
import os
import time
import urllib.request
from pathlib import Path
from urllib.error import URLError

LLAMACPP_URL = os.environ.get("LLAMACPP_URL", "http://llamacpp.llamacpp.svc.cluster.local:8080")
K8S_MCP_URL = os.environ.get(
    "K8S_MCP_URL", "http://kubernetes-mcp-server.kubernetes-mcp-server.svc.cluster.local:8080/mcp"
)
# llama.cpp's context is 8192 tokens total, shared between the agentic path's
# tool schemas (sent on every request), the running conversation, and the
# response budget - a single unbounded pods_get/resources_get YAML dump was
# enough by itself to blow a request past 8192 after just 2 tool calls. Cap
# each individual tool result; a truncated real answer is far more useful
# than a hard failure that aborts the whole investigation.
MAX_TOOL_RESULT_CHARS = 2000

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name, **kwargs):
    """Read a prompt template from prompts/ and fill in its placeholders."""
    text = (PROMPTS_DIR / name).read_text()
    return text.format(**kwargs) if kwargs else text


def urlopen_with_retry(req, timeout, attempts=6):
    """Connection-refused blips lasting several seconds, specifically on
    calls to llamacpp from the long-running server process (never reproduced
    via a one-off exec'd process against the same target), are a real,
    observed characteristic of this cluster - retry with a generous budget
    rather than lose the whole investigation to a multi-second hiccup."""
    last_exc = None
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except (URLError, OSError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(2.0 * (attempt + 1))
    raise last_exc


def chat_completion(messages, tools=None, max_tokens=1024, temperature=0):
    """Call llama.cpp's OpenAI-compatible endpoint, return the response message dict."""
    body = {
        "model": "local",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Greedy decoding (temperature=0) on a long, structurally-repetitive
        # evidence block (e.g. a Node object with several near-identical
        # condition entries) can get stuck re-emitting the same chunk until
        # max_tokens - observed live as the same MemoryPressure condition
        # repeated 7 times with no diagnosis text at all. repeat_penalty is
        # a llama.cpp server extension to the OpenAI schema, not standard,
        # but harmless to send to any OpenAI-compatible backend that ignores
        # unknown fields.
        "repeat_penalty": 1.15,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req = urllib.request.Request(
        LLAMACPP_URL + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen_with_retry(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]


async def call_tool_text(session, name, args, max_chars=MAX_TOOL_RESULT_CHARS):
    """Call an MCP tool and return its text content, truncated to protect
    llama.cpp's context window.

    max_chars=None skips truncation - for callers that only run pure-Python
    matching over the result (pod.py's resolve_pod_name, workload.py's
    resolve_controller_name) and never hand this text to the model. Confirmed
    live that the default cap silently broke both: pods_list_in_namespace for
    a busy namespace put the target pod past char 2000, and a Deployment
    listing did the same, leaving resolve_controller_name matching only
    against whatever survived truncation - it picked argocd-dex-server over
    the real argocd-repo-server because the correct name had already been cut
    off, not because the fuzzy match itself was wrong."""
    try:
        result = await session.call_tool(name, args)
        text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    except Exception as exc:  # noqa: BLE001 - surfaced as evidence, not raised
        text = f"tool error: {exc}"
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"
    return text
