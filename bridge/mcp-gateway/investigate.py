"""Dispatches an alert to the first matching deterministic playbook (see
playbooks/__init__.py), or falls back to agentic.py's open-ended loop if no
playbook claims it."""
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import agentic
from common import K8S_MCP_URL
from playbooks import PLAYBOOKS


async def run(alert_text, payload):
    async with streamablehttp_client(K8S_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for playbook in PLAYBOOKS:
                target = playbook.extract_target(payload)
                if target is not None:
                    return await playbook.investigate(session, alert_text, target)
            return await agentic.investigate(session, alert_text)


async def run_direct(playbook, alert_text, target):
    """Used by gateway.py's /ask endpoint - the playbook and target are
    already known (from intent classification, not extract_target(payload)),
    so this skips the PLAYBOOKS dispatch loop and agentic.py fallback
    entirely. Reuses the same MCP session setup as run()."""
    async with streamablehttp_client(K8S_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await playbook.investigate(session, alert_text, target)
