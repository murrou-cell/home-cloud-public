"""Shared HTTP/MCP helpers and config used by gateway.py, investigate.py, agentic.py, and the playbooks/ modules."""
import difflib
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.error import URLError

LLAMACPP_URL = os.environ.get("LLAMACPP_URL", "http://llamacpp.llamacpp.svc.cluster.local:8080")
K8S_MCP_URL = os.environ.get(
    "K8S_MCP_URL", "http://kubernetes-mcp-server.kubernetes-mcp-server.svc.cluster.local:8080/mcp"
)
GRAFANA_MCP_URL = os.environ.get(
    "GRAFANA_MCP_URL", "http://grafana-mcp-server.grafana-mcp-server.svc.cluster.local:8000/mcp"
)
PROXMOX_MCP_URL = os.environ.get(
    "PROXMOX_MCP_URL", "http://proxmox-mcp.proxmox-mcp.svc.cluster.local:8080/mcp"
)
PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL", "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"
)
# Caps each tool result so the agentic path's schemas + conversation don't blow llama.cpp's 8192-token context.
MAX_TOOL_RESULT_CHARS = 2000

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name, **kwargs):
    """Read a prompt template from prompts/ and fill in its placeholders."""
    text = (PROMPTS_DIR / name).read_text()
    return text.format(**kwargs) if kwargs else text


def urlopen_with_retry(req, timeout, attempts=6, context=None):
    """Retries connection blips (observed live against llamacpp) with backoff instead of losing a whole investigation."""
    last_exc = None
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=context)
        except (URLError, OSError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(2.0 * (attempt + 1))
    raise last_exc


def submit_workflow(template_name, namespace, parameters):
    """POST a Workflow via this pod's own ServiceAccount token, same mechanism Atlantis uses; only called when CLAUDE_ESCALATION_ENABLED."""
    token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
    body = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {"generateName": f"{template_name}-"},
        "spec": {
            "workflowTemplateRef": {"name": template_name},
            "arguments": {
                "parameters": [{"name": k, "value": v} for k, v in parameters.items()]
            },
        },
    }
    req = urllib.request.Request(
        f"https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/{namespace}/workflows",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    with urlopen_with_retry(req, timeout=15, attempts=2, context=context) as resp:
        return json.loads(resp.read())


def chat_completion(messages, tools=None, max_tokens=1024, temperature=0):
    """Call llama.cpp's OpenAI-compatible endpoint, return the response message dict."""
    body = {
        "model": "local",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Avoids greedy decoding getting stuck re-emitting repetitive evidence (observed live); harmless elsewhere.
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
    choice = data["choices"][0]
    message = choice["message"]
    # finish_reason="length" flags truncated-mid-generation output (e.g. a narrated fake tool call) so callers can detect it.
    message["finish_reason"] = choice.get("finish_reason")
    return message


async def call_tool_text(session, name, args, max_chars=MAX_TOOL_RESULT_CHARS):
    """Call an MCP tool and return its text, truncated; max_chars=None for callers that pattern-match the raw text instead of showing it to the model."""
    try:
        result = await session.call_tool(name, args)
        text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    except Exception as exc:  # noqa: BLE001 - surfaced as evidence, not raised
        text = f"tool error: {exc}"
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"
    return text


def truncate_keeping_status(text, max_chars):
    """Kubernetes status: is last and most diagnostically important - keep a short head plus the status section, not a flat prefix."""
    if len(text) <= max_chars:
        return text
    status_idx = text.find("\nstatus:")
    if status_idx == -1 or status_idx < max_chars:
        return text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"
    head_budget = max_chars // 3
    status_budget = max_chars - head_budget
    head = text[:head_budget]
    status_part = text[status_idx : status_idx + status_budget]
    omitted = f"\n... (truncated, {len(text)} chars total)" if status_idx + status_budget < len(text) else ""
    return f"{head}\n... (metadata/spec middle omitted) ...\n{status_part}{omitted}"


def extract_proxmox_memory_summary(status_json_text):
    """proxmox-mcp reports memory/swap/rootfs as raw byte counts; compute GiB deterministically instead of letting the model convert (it hallucinates)."""
    try:
        data = json.loads(status_json_text)
    except (json.JSONDecodeError, TypeError):
        return None
    parts = []
    for section, label in (("memory", "memory"), ("swap", "swap"), ("rootfs", "disk")):
        info = data.get(section)
        if not isinstance(info, dict) or "used" not in info or "total" not in info:
            continue
        used_gib = info["used"] / (1024**3)
        total_gib = info["total"] / (1024**3)
        parts.append(f"{label}: {used_gib:.2f} GiB used / {total_gib:.2f} GiB total")
    return ", ".join(parts) if parts else None


def summarize_node_pod_stats(stats_json_text, top_n=5):
    """nodes_stats_summary is ~170KB; extract the real top-N pods by memory deterministically instead of truncating and inviting a guess."""
    try:
        data = json.loads(stats_json_text)
    except (json.JSONDecodeError, TypeError):
        return "(could not parse node stats)"
    rows = []
    for pod in data.get("pods", []):
        mem = pod.get("memory", {}).get("workingSetBytes")
        if mem is None:
            continue
        ref = pod.get("podRef", {})
        cpu_cores = pod.get("cpu", {}).get("usageNanoCores")
        rows.append((mem, ref.get("namespace", "?"), ref.get("name", "?"), cpu_cores))
    if not rows:
        return "(no per-pod stats available)"
    rows.sort(key=lambda r: r[0], reverse=True)
    lines = [f"top {min(top_n, len(rows))} pods on this node by memory:"]
    for mem, namespace, name, cpu_cores in rows[:top_n]:
        cpu_str = f"{cpu_cores / 1e9:.3f} cores" if cpu_cores is not None else "unknown"
        lines.append(f"  {namespace}/{name}: {mem / (1024 * 1024):.0f} MiB, {cpu_str} CPU")
    return "\n".join(lines)


def extract_node_memory_summary(node_status_text):
    """resources_get Node reports memory as a raw KiB string; compute GiB deterministically instead of letting the model convert (it hallucinates)."""
    alloc_idx = node_status_text.find("allocatable:")
    cap_idx = node_status_text.find("capacity:")
    if alloc_idx == -1 or cap_idx == -1:
        return None
    alloc_block = node_status_text[alloc_idx:cap_idx] if alloc_idx < cap_idx else node_status_text[alloc_idx : alloc_idx + 300]
    cap_block = node_status_text[cap_idx : cap_idx + 300]

    def find_mem_gib(block):
        match = re.search(r'memory:\s*"?(\d+)Ki"?', block)
        return int(match.group(1)) / (1024 * 1024) if match else None

    alloc_gib = find_mem_gib(alloc_block)
    cap_gib = find_mem_gib(cap_block)
    if alloc_gib is None and cap_gib is None:
        return None
    parts = []
    if alloc_gib is not None:
        parts.append(f"allocatable memory: {alloc_gib:.2f} GiB")
    if cap_gib is not None:
        parts.append(f"capacity memory: {cap_gib:.2f} GiB")
    return ", ".join(parts)


# Strips a trailing hash suffix to get the underlying workload name; excludes vowels so real words (hashes never contain one) aren't stripped too.
HASH_SUFFIX_RE = re.compile(r"(-[bcdfghjklmnpqrstvwxyz0-9]{5,10}){1,2}$")


def resolve_resource_namespace(name, resources_text):
    """Cluster-wide name -> (namespace, name) resolution (substring match, then word-overlap fallback); returns ("ambiguous", {...}) instead of guessing between distinct matches, ("resolved", (namespace, name)), or None."""
    stem = re.sub(HASH_SUFFIX_RE, "", name)
    by_name = {}
    for line in resources_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and stem in parts[3]:
            by_name[parts[3]] = parts[0]
    if not by_name:
        words = {w for w in stem.lower().split() if len(w) > 2}
        if words:
            scores = {}
            for line in resources_text.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    overlap = len(words & set(parts[3].lower().split("-")))
                    if overlap:
                        scores[parts[3]] = (overlap, parts[0])
            if scores:
                best_score = max(overlap for overlap, _ in scores.values())
                by_name = {n: ns for n, (overlap, ns) in scores.items() if overlap == best_score}
    if not by_name:
        return None
    groups = {}
    for n, ns in by_name.items():
        own_stem = re.sub(HASH_SUFFIX_RE, "", n)
        groups.setdefault(own_stem, (n, ns))
    if len(groups) > 1:
        return "ambiguous", dict(groups.values())
    best = max(by_name, key=lambda c: difflib.SequenceMatcher(None, name, c).ratio())
    return "resolved", (by_name[best], best)


def query_prometheus_scalar(promql):
    """Run an instant PromQL query, return the first result's float or None; kubernetes-mcp-server has no path to real PVC usage."""
    url = f"{PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(promql)}"
    try:
        with urlopen_with_retry(urllib.request.Request(url), timeout=10, attempts=2) as resp:
            data = json.loads(resp.read())
        result = data.get("data", {}).get("result") or []
        return float(result[0]["value"][1]) if result else None
    except Exception:  # noqa: BLE001 - absence of data is a valid, common outcome
        return None
