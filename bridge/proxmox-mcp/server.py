"""Read-only MCP server exposing Proxmox VE cluster state (VM/node status,
resource usage, task log) to mcp-gateway's playbooks. Deliberately narrow:
no console/exec, no config mutation, no snapshot/clone/delete - the
mcp-readonly@pve API token this authenticates with only has VM.Audit,
Sys.Audit, Datastore.Audit, Pool.Audit privileges, so a compromised or
misbehaving MCP session can't do more than this tool set already limits it
to even if it tried."""
import json
import os
import ssl
import urllib.request
from urllib.error import URLError

from mcp.server.fastmcp import FastMCP

PROXMOX_API_URL = os.environ["PROXMOX_API_URL"]
PROXMOX_TOKEN_ID = os.environ["PROXMOX_TOKEN_ID"]
PROXMOX_TOKEN_SECRET = os.environ["PROXMOX_TOKEN_SECRET"]

# Proxmox's own web UI cert is self-signed in this homelab (same class of
# fix as Grafana's tls_skip_verify_insecure for its Dex OAuth token/userinfo
# calls, argocd/apps/kube-prometheus-stack/values.yaml) - this is a
# cluster-internal call over the LAN, not a public-facing request.
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE

mcp = FastMCP("proxmox-mcp", host="0.0.0.0", port=8080, stateless_http=True)


def _proxmox_get(path):
    """GET a Proxmox API path, return the `data` field. Raises on failure -
    callers are individual @mcp.tool() functions, so an exception here
    becomes a tool-call error the model sees, not a crashed process."""
    req = urllib.request.Request(
        f"{PROXMOX_API_URL}/api2/json{path}",
        headers={"Authorization": f"PVEAPIToken={PROXMOX_TOKEN_ID}={PROXMOX_TOKEN_SECRET}"},
    )
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read())["data"]


@mcp.tool()
def list_nodes() -> str:
    """List every Proxmox node in the cluster with its name, corosync ring IP, online status, and quorum info.
    Use this to resolve a bare node IP (e.g. from a node-exporter alert's `instance` label) to the Proxmox
    node name get_node_status/list_vms/list_recent_tasks expect."""
    return json.dumps([n for n in _proxmox_get("/cluster/status") if n.get("type") == "node"])


@mcp.tool()
def get_node_status(node: str) -> str:
    """Get detailed resource usage (CPU, memory, load average, kernel version, uptime) for one Proxmox node."""
    return json.dumps(_proxmox_get(f"/nodes/{node}/status"))


@mcp.tool()
def list_vms(node: str | None = None) -> str:
    """List VMs (QEMU guests) and their status (running/stopped), CPU/memory usage. Pass node to scope to one Proxmox node, or omit to list VMs across every node in the cluster."""
    nodes = [node] if node else [n["node"] for n in _proxmox_get("/nodes")]
    vms = []
    for n in nodes:
        try:
            for vm in _proxmox_get(f"/nodes/{n}/qemu"):
                vm["node"] = n
                vms.append(vm)
        except (URLError, OSError, KeyError, ValueError) as exc:
            vms.append({"node": n, "error": str(exc)})
    return json.dumps(vms)


@mcp.tool()
def get_vm_status(vmid: int, node: str) -> str:
    """Get a detailed live status snapshot (CPU, memory, disk I/O, network I/O) for one VM. Requires both the VM ID and the Proxmox node it's running on (see list_vms)."""
    return json.dumps(_proxmox_get(f"/nodes/{node}/qemu/{vmid}/status/current"))


@mcp.tool()
def list_recent_tasks(node: str | None = None, limit: int = 20) -> str:
    """List recent Proxmox task-log entries (backups, migrations, VM start/stop, errors) - the one thing genuinely unavailable anywhere else in this pipeline. Pass node to scope to one Proxmox node, or omit to merge recent tasks across every node."""
    nodes = [node] if node else [n["node"] for n in _proxmox_get("/nodes")]
    tasks = []
    for n in nodes:
        try:
            tasks.extend(_proxmox_get(f"/nodes/{n}/tasks?limit={limit}"))
        except (URLError, OSError, KeyError, ValueError):
            continue
    tasks.sort(key=lambda t: t.get("starttime", 0), reverse=True)
    return json.dumps(tasks[:limit])


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
