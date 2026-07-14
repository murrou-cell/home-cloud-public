"""Dispatches an alert to the first matching deterministic playbook, or falls back to agentic.py's open-ended loop."""
import contextlib

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import agentic
from common import GRAFANA_MCP_URL, K8S_MCP_URL, PROXMOX_MCP_URL
from playbooks import PLAYBOOKS


async def open_optional_session(exit_stack, url):
    """Best-effort optional MCP session (grafana-mcp-server/proxmox-mcp) - returns None if unreachable, using a local exit stack so a failed attempt can't crash the investigation."""
    try:
        async with contextlib.AsyncExitStack() as local_stack:
            read, write, _ = await local_stack.enter_async_context(streamablehttp_client(url))
            session = await local_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            await exit_stack.enter_async_context(local_stack.pop_all())
            return session
    except Exception:  # noqa: BLE001 - an optional tool server being down must never abort the investigation
        return None


async def run(alert_text, payload):
    """Returns (diagnosis, target_file); target_file is the playbook module path that handled it, or None if the agentic fallback ran instead."""
    async with streamablehttp_client(K8S_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for playbook in PLAYBOOKS:
                target = playbook.extract_target(payload)
                if target is not None:
                    diagnosis = await playbook.investigate(session, alert_text, target)
                    return diagnosis, f"bridge/mcp-gateway/playbooks/{playbook.NAME}.py"
            # No fixed playbook claims this shape - fall back to the open-ended loop with whichever optional servers are reachable.
            async with contextlib.AsyncExitStack() as stack:
                sessions = [session]
                for url in (GRAFANA_MCP_URL, PROXMOX_MCP_URL):
                    extra = await open_optional_session(stack, url)
                    if extra is not None:
                        sessions.append(extra)
                diagnosis = await agentic.investigate(sessions, alert_text)
                return diagnosis, None


async def run_direct(playbook, alert_text, target):
    """Used by /ask - playbook/target are already known, so this skips the PLAYBOOKS dispatch loop and agentic fallback entirely."""
    mcp_url = getattr(playbook, "MCP_URL", K8S_MCP_URL)
    async with streamablehttp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await playbook.investigate(session, alert_text, target)
