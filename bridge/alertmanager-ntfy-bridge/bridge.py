#!/usr/bin/env python3
"""Translates Alertmanager's fixed webhook JSON into ntfy's publish schema.

One route per severity: /critical, /warning, /info.
"""
import base64
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NTFY_URL = os.environ.get("NTFY_URL", "http://ntfy.ntfy.svc.cluster.local")
NTFY_USER = os.environ.get("NTFY_PUBLISHER_USER", "ntfy-publisher")
NTFY_PASSWORD = os.environ["NTFY_PUBLISHER_PASSWORD"]
MAX_BODY_BYTES = 1_000_000

ROUTES = {
    "/critical": {"topic": "pf-alerts-critical", "priority": 5, "tag": "rotating_light"},
    "/warning": {"topic": "pf-alerts-warning", "priority": 3, "tag": "warning"},
    "/info": {"topic": "pf-alerts-info", "priority": 2, "tag": "information_source"},
}


def build_message(payload, tag):
    alerts = payload.get("alerts") or []
    resolved = payload.get("status") == "resolved"
    names = sorted({a.get("labels", {}).get("alertname", "alert") for a in alerts}) or ["alert"]
    title = ("RESOLVED: " if resolved else "") + ", ".join(names)

    lines = []
    for a in alerts:
        ann = a.get("annotations", {})
        text = ann.get("summary") or ann.get("description") or a.get("labels", {}).get("alertname", "")
        lines.append(text)
    message = "\n".join(lines) or "(no details)"

    return title, message, "white_check_mark" if resolved else tag


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        route = ROUTES.get(self.path)
        if route is None:
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
            # Close explicitly rather than draining an attacker-controlled length.
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

        title, message, tag = build_message(payload, route["tag"])
        ntfy_body = json.dumps(
            {
                "topic": route["topic"],
                "title": title,
                "message": message,
                "priority": route["priority"],
                "tags": [tag],
            }
        ).encode()

        auth = base64.b64encode(f"{NTFY_USER}:{NTFY_PASSWORD}".encode()).decode()
        req = urllib.request.Request(
            NTFY_URL + "/",
            data=ntfy_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {auth}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001 — surface as 502 to Alertmanager for retry
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(exc).encode())
            return

        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        print(fmt % args)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
