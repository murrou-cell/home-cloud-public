"""Live Claude-backed investigation for /ask, when the local model's answer isn't good enough - read-only MCP tool access only, never mutates the cluster, never drafts a PR."""
import os

import anthropic

CLAUDE_ASK_MODEL = os.environ.get("CLAUDE_ASK_MODEL", "claude-sonnet-5")
MAX_TOOL_TURNS = 8

SYSTEM_PROMPT = (
    "You are a read-only Kubernetes/Proxmox/Grafana homelab assistant. Use the tools available to you to gather "
    "real evidence about this specific cluster before answering - do not substitute general Kubernetes knowledge "
    "for actually checking. You cannot execute commands or change anything, only read. State only what the tool "
    "results actually showed. Give a clear, direct answer once you have enough evidence; if you genuinely can't "
    "find an answer after investigating, say so honestly rather than guessing."
)


def mcp_tool_to_anthropic(tool):
    return {"name": tool.name, "description": tool.description or "", "input_schema": tool.inputSchema}


async def investigate(sessions, question):
    """sessions: one MCP session or a list; tool schemas are merged and each call routed back to whichever session owns that tool name."""
    if not isinstance(sessions, (list, tuple)):
        sessions = [sessions]

    tools = []
    tool_owner = {}
    for session in sessions:
        tools_resp = await session.list_tools()
        for t in tools_resp.tools:
            tools.append(mcp_tool_to_anthropic(t))
            tool_owner[t.name] = session

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": question}]

    for _ in range(MAX_TOOL_TURNS):
        response = client.messages.create(
            model=CLAUDE_ASK_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text = "\n".join(b.text for b in response.content if b.type == "text")
            return text or "(Claude returned no answer text)"

        tool_results = []
        for call in tool_uses:
            owner = tool_owner.get(call.name)
            if owner is None:
                content = f"tool error: unknown tool {call.name!r}"
            else:
                try:
                    result = await owner.call_tool(call.name, call.input)
                    content = "\n".join(c.text for c in result.content if hasattr(c, "text"))
                except Exception as exc:  # noqa: BLE001 - surfaced as evidence, not raised
                    content = f"tool error: {exc}"
            tool_results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
        messages.append({"role": "user", "content": tool_results})

    return "Claude did not conclude within the tool-call budget."
