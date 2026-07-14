#!/usr/bin/env python3
"""Alert-triggered cluster investigation.

Receives the same Alertmanager webhook payload the ntfy bridge gets, asks
llama.cpp a one-word triage question (worth investigating or not), and for
anything flagged ESCALATE, dispatches to investigate.run() - a deterministic
playbook (playbooks/) if the alert shape matches one, otherwise agentic.py's
open-ended tool-use loop. Diagnosis is posted to ntfy. Entirely local: no
external API, no cost. See playbooks/__init__.py for how to add a new alert
shape, and prompts/ for the actual model instructions.
"""
import asyncio
import base64
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import LLAMACPP_URL, chat_completion, load_prompt, urlopen_with_retry

# Read once at import time, not per-request - the file is small and static,
# no reason to hit the filesystem on every GET /. Not loaded via
# common.load_prompt(): that calls str.format() on the file contents, which
# would choke on every literal {} in the page's own CSS/JS (same class of
# bug already hit once with intent_classify.txt's JSON example).
ASK_UI_HTML = (Path(__file__).parent / "static" / "index.html").read_text()
from investigate import run as run_investigation
from investigate import run_direct
from playbooks import argocd, host, pod, pvc, workload

# question intent -> (playbook module, required fields, apiVersion/kind lookup
# for the "workload" intent only). Kept separate from playbooks/__init__.py's
# PLAYBOOKS list since that's for alert-shape dispatch (extract_target from
# Alertmanager labels) - this is question-shape dispatch (from intent
# classification), a different target-building path into the same playbooks.
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
# Hard ceiling on investigations per rolling hour. Not a cost concern (this is
# all local now) but the RX 580 is a single shared, already-contended GPU -
# this stops an alert storm (many namespaces firing at once, each its own
# Alertmanager group) from queuing up a pile of concurrent tool-use loops.
MAX_INVESTIGATIONS_PER_HOUR = int(os.environ.get("MAX_INVESTIGATIONS_PER_HOUR", "10"))

_warmup_done = threading.Event()
_investigation_times = []
_budget_lock = threading.Lock()


def budget_available():
    now = time.monotonic()
    with _budget_lock:
        global _investigation_times
        _investigation_times = [t for t in _investigation_times if now - t < 3600]
        if len(_investigation_times) >= MAX_INVESTIGATIONS_PER_HOUR:
            return False
        _investigation_times.append(now)
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
        # Only alertname/summary/severity, not the full label dump: labels
        # like container/instance/job/pod/service/endpoint usually identify
        # the *scrape target that produced the metric* (e.g. kube-state-metrics'
        # own pod), not the resource the alert is actually about - live testing
        # showed the model conflating "container=kube-state-metrics" from a
        # KubeDeploymentReplicasMismatch alert with the actual Deployment being
        # investigated, claiming it had a "kube-state-metrics container". Each
        # playbook's evidence already restates the real target's name/namespace/
        # kind in its own section headers, so the raw labels add noise here, not
        # signal.
        lines.append(f"{name}: {summary}" + (f" (severity={severity})" if severity else ""))
    return "\n".join(lines) or "(no alert details)"


def triage(alert_text):
    """Ask llama.cpp whether this alert is worth a deeper investigation."""
    prompt = load_prompt("triage.txt", alert_text=alert_text)
    message = chat_completion([{"role": "user", "content": prompt}], max_tokens=8)
    verdict = (message.get("content") or "").strip().upper()
    return verdict.startswith("ESCALATE")


SUPPORTED_INTENTS_MESSAGE = (
    "I can only answer questions about a specific pod's health, a specific "
    "node/host's memory or CPU, a specific PersistentVolumeClaim's space, a "
    "specific Argo CD Application's sync status, or a specific Deployment/"
    "DaemonSet/StatefulSet/Job/PodDisruptionBudget's health - and I need the "
    "actual name, not a general question."
)


def classify_intent(question):
    """Ask llama.cpp to map a question to one of the known playbook shapes,
    constrained to a fixed JSON schema. Never trust the model's own claim
    that it filled every required field - build_target() re-validates with
    plain truthiness checks, since live testing showed the model sometimes
    returning e.g. {"intent": "pod", "namespace": "", "name": ""} instead of
    honestly returning {"intent": null} when it couldn't confidently answer."""
    prompt = load_prompt("intent_classify.txt", question=question)
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
    """Turn a classify_intent() result into (playbook_module, target) for
    investigate.run_direct(), or (None, None) if the intent is unsupported
    or missing a required field - required fields are re-checked here with
    plain truthiness, not just presence, deliberately not trusting the
    model's own judgment that it filled them in correctly."""
    intent = intent_data.get("intent")

    if intent == "pod":
        namespace, name = intent_data.get("namespace"), intent_data.get("name")
        if namespace and name:
            return pod, {"namespace": namespace, "pod": name}

    elif intent == "host":
        name = intent_data.get("name")
        if name:
            return host, {"instance": None, "node_label": name, "cluster_label": None}

    elif intent == "argocd":
        name = intent_data.get("name")
        if name:
            return argocd, {"name": name}

    elif intent == "pvc":
        namespace, name = intent_data.get("namespace"), intent_data.get("name")
        if namespace and name:
            return pvc, {"namespace": namespace, "pvc": name}

    elif intent == "workload":
        namespace, name, kind = intent_data.get("namespace"), intent_data.get("name"), intent_data.get("kind")
        api_version = WORKLOAD_KIND_API_VERSIONS.get(kind)
        if namespace and name and api_version:
            return workload, {"namespace": namespace, "name": name, "apiVersion": api_version, "kind": kind}

    return None, None


def handle_ask(question):
    intent_data = classify_intent(question)
    module, target = build_target(intent_data)
    if not module:
        return SUPPORTED_INTENTS_MESSAGE
    if not budget_available():
        return f"Hourly investigation budget ({MAX_INVESTIGATIONS_PER_HOUR}) is exhausted right now - try again later."
    alert_text = f"(user question, not an alert): {question}"
    return asyncio.run(run_direct(module, alert_text, target))


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
        for attempt in range(2):
            try:
                print(f"stage: investigate attempt {attempt}")
                diagnosis = asyncio.run(run_investigation(alert_text, payload))
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
    except Exception as exc:  # noqa: BLE001 - this runs off the request thread, only stdout can see it
        import traceback

        print(f"handle_alert failed: {exc!r}")
        traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/investigate", "/ask"):
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

        if self.path == "/ask":
            # Synchronous, unlike /investigate: there's no webhook timeout to
            # race, and the caller (a chat UI) wants the answer in the
            # response body, not delivered later via ntfy.
            question = (payload.get("question") or "").strip()
            if not question:
                self.send_response(400)
                self.end_headers()
                return
            try:
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

        # Ack Alertmanager immediately - investigation can take well past its webhook
        # timeout, the diagnosis is delivered later via ntfy instead of in this response.
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
            # Liveness: process is up and serving. Separate from readiness -
            # never gate this on warm_up(), or a slow warm-up trips the
            # liveness probe's failure threshold and crash-loops the pod.
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
    """The first cross-node connection from a freshly-started pod to llamacpp
    (mcp-gateway runs on k3s-worker, llamacpp on k3s-gpu) has been observed to
    fail with ECONNREFUSED for 30+ seconds - neither pod's own readiness probe
    exercises that specific node-to-node path, since kubelet always probes
    locally. Pay that cold-start cost here at boot instead of on the first
    real alert."""
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
