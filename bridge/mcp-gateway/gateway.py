#!/usr/bin/env python3
"""Alert-triggered cluster investigation: triage via llama.cpp, then a deterministic playbook or agentic.py's fallback, posted to ntfy."""
import asyncio
import base64
import contextlib
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import claude_ask
from common import (
    GRAFANA_MCP_URL,
    K8S_MCP_URL,
    LLAMACPP_URL,
    PROXMOX_MCP_URL,
    call_tool_text,
    chat_completion,
    load_prompt,
    submit_workflow,
    urlopen_with_retry,
)

# Read once at import time; not loaded via load_prompt() since that does str.format() and breaks on the page's own {}.
ASK_UI_HTML = (Path(__file__).parent / "static" / "index.html").read_text()
from investigate import open_optional_session
from investigate import run as run_investigation
from investigate import run_direct
from playbooks import argocd, grafana, host, list_resource, pod, proxmox, pvc, workload

# kind -> apiVersion for the "workload" /ask intent; separate from playbooks/__init__.py's alert-shape dispatch.
WORKLOAD_KIND_API_VERSIONS = {
    "Deployment": "apps/v1",
    "DaemonSet": "apps/v1",
    "StatefulSet": "apps/v1",
    "Job": "batch/v1",
    "PodDisruptionBudget": "policy/v1",
}

NTFY_URL = os.environ.get("NTFY_URL", "http://ntfy.ntfy.svc.cluster.local")
NTFY_USER = os.environ.get("NTFY_PUBLISHER_USER", "ntfy-publisher")
NTFY_PASSWORD = os.environ["NTFY_PUBLISHER_PASSWORD"]
NTFY_TOPIC = os.environ.get("NTFY_DIAGNOSIS_TOPIC", "pf-alerts-diagnosis")
MAX_BODY_BYTES = 1_000_000
# Ceiling on investigations/hour - protects the single shared GPU from an alert storm, not a cost concern.
MAX_INVESTIGATIONS_PER_HOUR = int(os.environ.get("MAX_INVESTIGATIONS_PER_HOUR", "10"))

# Kill switch for Claude escalation (argocd/apps/mcp-gateway/values.yaml) - off until Anthropic billing exists.
CLAUDE_ESCALATION_ENABLED = os.environ.get("CLAUDE_ESCALATION_ENABLED", "false").lower() == "true"

# Separate kill switch + budget for the live "ask Claude directly" /ask/escalate path - user-triggered (a button
# click), read-only, never drafts a PR, so a different risk/cost profile from CLAUDE_ESCALATION_ENABLED above.
CLAUDE_ASK_ENABLED = os.environ.get("CLAUDE_ASK_ENABLED", "false").lower() == "true"
MAX_CLAUDE_ASK_PER_HOUR = int(os.environ.get("MAX_CLAUDE_ASK_PER_HOUR", "10"))
SUPPORTED_INTENTS_MESSAGE = (
    "I can only answer questions about a specific pod's health, a specific "
    "Kubernetes node/host's memory or CPU, a specific PersistentVolumeClaim's "
    "space, a specific Argo CD Application's sync status, a specific Deployment/"
    "DaemonSet/StatefulSet/Job/PodDisruptionBudget's health, a specific Proxmox "
    "host's status, what Grafana dashboard covers a topic, or how many/which "
    "nodes/namespaces/pods/PVCs/Deployments/DaemonSets/StatefulSets/Jobs/"
    "PodDisruptionBudgets/Argo CD Applications/Proxmox hosts exist - and I "
    "need the actual name for a specific-resource question, not a vague one."
)

# Sentinels meaning the pipeline couldn't reach a confident answer - triggers maybe_escalate_to_claude() below.
LOW_CONFIDENCE_SENTINELS = (
    "Investigation did not conclude within the tool-call budget.",
    "(model returned no diagnosis text)",
    "INSUFFICIENT_EVIDENCE:",
    "(model response was truncated before completing a real answer",
    "(ran out of context budget partway through the investigation",
    SUPPORTED_INTENTS_MESSAGE,
)

_warmup_done = threading.Event()
_investigation_times = []
_budget_lock = threading.Lock()
_claude_ask_times = []
_claude_ask_budget_lock = threading.Lock()


def budget_available():
    now = time.monotonic()
    with _budget_lock:
        global _investigation_times
        _investigation_times = [t for t in _investigation_times if now - t < 3600]
        if len(_investigation_times) >= MAX_INVESTIGATIONS_PER_HOUR:
            return False
        _investigation_times.append(now)
        return True


def claude_ask_budget_available():
    now = time.monotonic()
    with _claude_ask_budget_lock:
        global _claude_ask_times
        _claude_ask_times = [t for t in _claude_ask_times if now - t < 3600]
        if len(_claude_ask_times) >= MAX_CLAUDE_ASK_PER_HOUR:
            return False
        _claude_ask_times.append(now)
        return True


def build_alert_text(payload):
    alerts = payload.get("alerts") or []
    lines = []
    for a in alerts:
        labels = a.get("labels", {})
        ann = a.get("annotations", {})
        name = labels.get("alertname", "alert")
        summary = ann.get("summary") or ann.get("description") or ""
        severity = labels.get("severity")
        # Only alertname/summary/severity - raw labels are the scrape target, not the actual resource, and confuse the model.
        lines.append(f"{name}: {summary}" + (f" (severity={severity})" if severity else ""))
    return "\n".join(lines) or "(no alert details)"


def triage(alert_text):
    """Ask llama.cpp whether this alert is worth a deeper investigation."""
    prompt = load_prompt("triage.txt", alert_text=alert_text)
    message = chat_completion([{"role": "user", "content": prompt}], max_tokens=8)
    verdict = (message.get("content") or "").strip().upper()
    return verdict.startswith("ESCALATE")


async def fetch_proxmox_hosts():
    """Live Proxmox host list for the classifier, fetched fresh each time - never hardcoded, best-effort."""
    try:
        async with streamablehttp_client(PROXMOX_MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                text = await call_tool_text(session, "list_nodes", {})
                return [n["name"] for n in json.loads(text) if n.get("name")]
    except Exception:  # noqa: BLE001 - grounding is optional, never fatal to classification
        return []


async def fetch_k8s_nodes():
    """Live Kubernetes node list for the classifier, fetched fresh each time - symmetric with fetch_proxmox_hosts()."""
    try:
        async with streamablehttp_client(K8S_MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                text = await call_tool_text(
                    session, "resources_list", {"apiVersion": "v1", "kind": "Node"}, max_chars=None
                )
        return [parts[2] for line in text.splitlines()[1:] if len(parts := line.split()) > 2]
    except Exception:  # noqa: BLE001 - grounding is optional, never fatal to classification
        return []


def classify_intent(question, proxmox_hosts, k8s_nodes):
    """Ask llama.cpp to classify the question; build_target() re-validates required fields, never trusts it blindly."""
    proxmox_hosts_hint = (
        f"The bare-metal Proxmox hosts that currently exist in this cluster are: "
        f"{', '.join(proxmox_hosts)}."
        if proxmox_hosts
        else ""
    )
    k8s_nodes_hint = (
        f"The Kubernetes nodes that currently exist in this cluster are: {', '.join(k8s_nodes)}."
        if k8s_nodes
        else ""
    )
    prompt = load_prompt(
        "intent_classify.txt",
        question=question,
        proxmox_hosts_hint=proxmox_hosts_hint,
        k8s_nodes_hint=k8s_nodes_hint,
    )
    message = chat_completion([{"role": "user", "content": prompt}], max_tokens=200)
    content = (message.get("content") or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.split("\n", 1)[-1] if "\n" in content else content
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {"intent": None}
    return data if isinstance(data, dict) else {"intent": None}


def build_target(intent_data):
    """Turn classify_intent()'s result into (playbook, target), or (None, None); re-checks required fields itself."""
    intent = intent_data.get("intent")

    if intent == "pod":
        namespace, name = intent_data.get("namespace"), intent_data.get("name")
        if name:
            return pod, {"namespace": namespace or None, "pod": name}

    elif intent == "host":
        name = intent_data.get("name")
        if name:
            return host, {"instance": None, "node_label": name, "cluster_label": None}

    elif intent == "argocd":
        name = intent_data.get("name")
        if name:
            return argocd, {"name": name}

    elif intent == "pvc":
        # namespace optional, like "pod" - pvc.py resolves it live when omitted.
        namespace, name = intent_data.get("namespace"), intent_data.get("name")
        if name:
            return pvc, {"namespace": namespace or None, "pvc": name}

    elif intent == "workload":
        namespace, name, kind = intent_data.get("namespace"), intent_data.get("name"), intent_data.get("kind")
        api_version = WORKLOAD_KIND_API_VERSIONS.get(kind)
        if namespace and name and api_version:
            return workload, {"namespace": namespace, "name": name, "apiVersion": api_version, "kind": kind}

    elif intent == "proxmox":
        # Host validity is checked live inside proxmox.py's investigate(), not here.
        node = intent_data.get("node")
        if node:
            return proxmox, {"node": node}

    elif intent == "list":
        resource = intent_data.get("resource")
        if resource in list_resource.K8S_RESOURCES or resource == "proxmox_hosts":
            return list_resource, {"resource": resource}

    elif intent == "grafana":
        query = intent_data.get("query")
        if query:
            return grafana, {"query": query}

    return None, None


async def fetch_classifier_hints():
    """Fetch both live lists concurrently - one event loop instead of two."""
    return await asyncio.gather(fetch_proxmox_hosts(), fetch_k8s_nodes())


def handle_ask(question):
    proxmox_hosts, k8s_nodes = asyncio.run(fetch_classifier_hints())
    intent_data = classify_intent(question, proxmox_hosts, k8s_nodes)
    module, target = build_target(intent_data)
    alert_text = f"(user question, not an alert): {question}"
    if not module:
        # No intent matched - escalate in the background so the caller isn't delayed by it.
        threading.Thread(
            target=maybe_escalate_to_claude, args=("ask", alert_text, SUPPORTED_INTENTS_MESSAGE, None), daemon=True
        ).start()
        return SUPPORTED_INTENTS_MESSAGE
    if not budget_available():
        return f"Hourly investigation budget ({MAX_INVESTIGATIONS_PER_HOUR}) is exhausted right now - try again later."
    answer = asyncio.run(run_direct(module, alert_text, target))
    target_file = f"bridge/mcp-gateway/playbooks/{module.NAME}.py"
    threading.Thread(target=maybe_escalate_to_claude, args=("ask", alert_text, answer, target_file), daemon=True).start()
    return answer


async def run_claude_ask(question):
    """Opens the same read-only MCP sessions the local agentic fallback uses, then hands the question to claude_ask.py."""
    async with streamablehttp_client(K8S_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            async with contextlib.AsyncExitStack() as stack:
                sessions = [session]
                for url in (GRAFANA_MCP_URL, PROXMOX_MCP_URL):
                    extra = await open_optional_session(stack, url)
                    if extra is not None:
                        sessions.append(extra)
                return await claude_ask.investigate(sessions, question)


def handle_ask_escalate(question):
    if not CLAUDE_ASK_ENABLED:
        return "Asking Claude directly isn't enabled right now."
    if not claude_ask_budget_available():
        return f"Hourly Claude-question budget ({MAX_CLAUDE_ASK_PER_HOUR}) is exhausted right now - try again later."
    try:
        return asyncio.run(run_claude_ask(question))
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not just stdout
        import traceback

        traceback.print_exc()
        return f"Something went wrong asking Claude: {exc!r}"


def publish_ntfy(title, message):
    body = json.dumps(
        {
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message,
            "priority": 3,
            "tags": ["mag"],
        }
    ).encode()
    auth = base64.b64encode(f"{NTFY_USER}:{NTFY_PASSWORD}".encode()).decode()
    req = urllib.request.Request(
        NTFY_URL + "/",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )
    with urlopen_with_retry(req, timeout=10) as resp:
        resp.read()


def maybe_escalate_to_claude(alertname, alert_text, diagnosis, target_file):
    """If the local diagnosis was low-confidence, submit a Workflow asking Claude to propose a playbook-fix PR; gated by CLAUDE_ESCALATION_ENABLED."""
    if not CLAUDE_ESCALATION_ENABLED:
        return
    if not any(sentinel in diagnosis for sentinel in LOW_CONFIDENCE_SENTINELS):
        return
    try:
        print("stage: claude escalation - submitting workflow")
        result = submit_workflow(
            "claude-improve-playbook",
            "argo-workflows",
            {
                "alertname": alertname,
                "alert_text": alert_text,
                "diagnosis": diagnosis,
                "target_file": target_file or "",
            },
        )
        workflow_name = result.get("metadata", {}).get("name", "?")
        print(f"stage: claude escalation - submitted {workflow_name}")
        publish_ntfy(
            f"Claude escalation: {alertname}",
            f"Local diagnosis was low-confidence - {workflow_name} is drafting a playbook "
            f"improvement PR for review.",
        )
    except Exception as exc:  # noqa: BLE001 - escalation failing must never affect the alert path above
        print(f"stage: claude escalation failed: {exc!r}")


def handle_alert(payload):
    alert_text = build_alert_text(payload)
    alertname = payload.get("commonLabels", {}).get("alertname") or "alert"
    try:
        print("stage: triage start")
        escalate = triage(alert_text)
        print(f"stage: triage done, escalate={escalate}")
        if not escalate:
            return
        if not budget_available():
            publish_ntfy(
                f"Budget exhausted: {alertname}",
                f"Hourly investigation budget ({MAX_INVESTIGATIONS_PER_HOUR}) is exhausted - skipped.",
            )
            return
        diagnosis = None
        target_file = None
        for attempt in range(2):
            try:
                print(f"stage: investigate attempt {attempt}")
                diagnosis, target_file = asyncio.run(run_investigation(alert_text, payload))
                print("stage: investigate done")
                break
            except OSError as exc:
                print(f"stage: investigate attempt {attempt} failed: {exc!r}")
                if attempt == 1:
                    raise
                time.sleep(2)
        print("stage: publish_ntfy start")
        publish_ntfy("Diagnosis: " + alertname, diagnosis)
        print("stage: publish_ntfy done")
        maybe_escalate_to_claude(alertname, alert_text, diagnosis, target_file)
    except Exception as exc:  # noqa: BLE001 - this runs off the request thread, only stdout can see it
        import traceback

        print(f"handle_alert failed: {exc!r}")
        traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/investigate", "/ask", "/ask/escalate"):
            self.send_response(404)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        if length > MAX_BODY_BYTES:
            self.send_response(413)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        if self.path in ("/ask", "/ask/escalate"):
            # Synchronous, unlike /investigate - the caller wants the answer in the response body, not via ntfy.
            question = (payload.get("question") or "").strip()
            if not question:
                self.send_response(400)
                self.end_headers()
                return
            try:
                if self.path == "/ask/escalate":
                    answer = handle_ask_escalate(question)
                else:
                    answer = handle_ask(question)
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not just stdout
                import traceback

                traceback.print_exc()
                answer = f"Something went wrong answering that: {exc!r}"
            response = json.dumps({"answer": answer}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        # Ack Alertmanager immediately; diagnosis is delivered later via ntfy.
        self.send_response(200)
        self.end_headers()
        threading.Thread(target=handle_alert, args=(payload,), daemon=True).start()

    def do_GET(self):
        if self.path == "/":
            body = ASK_UI_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/healthz":
            # Liveness only - never gated on warm_up(), or a slow warm-up trips the restart threshold.
            self.send_response(200)
            self.end_headers()
            return
        if self.path == "/readyz":
            self.send_response(200 if _warmup_done.is_set() else 503)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        print(fmt % args)


def warm_up():
    """First cross-node call to llamacpp can take 30s+ cold; pay that cost at boot, not on the first real alert."""
    try:
        req = urllib.request.Request(LLAMACPP_URL + "/health")
        urlopen_with_retry(req, timeout=10, attempts=8)
        print("warm-up: llamacpp reachable")
    except Exception as exc:  # noqa: BLE001 - best-effort, the real request will retry anyway
        print(f"warm-up: llamacpp still unreachable after warm-up attempts: {exc!r}")
    finally:
        _warmup_done.set()


if __name__ == "__main__":
    threading.Thread(target=warm_up, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
