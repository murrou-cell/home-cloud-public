"""Open-ended tool-use loop against llama.cpp's function calling - the fallback when no playbook claims an alert."""
import json
import urllib.error

from common import call_tool_text, chat_completion, load_prompt

# Bumped from 8: the repeat-call guard below can cost a turn without producing new evidence.
MAX_TOOL_TURNS = 12
# Forces a couple of verifying tool calls instead of guessing from one log line - there's no human to ask.
MIN_TOOL_CALLS = 3


def strip_unsupported_schema_keywords(schema):
    """llama.cpp's grammar converter can't parse the "pattern" JSON-schema keyword and 400s; strip it, it's just a validation hint."""
    if isinstance(schema, dict):
        return {
            k: strip_unsupported_schema_keywords(v)
            for k, v in schema.items()
            if k != "pattern"
        }
    if isinstance(schema, list):
        return [strip_unsupported_schema_keywords(v) for v in schema]
    return schema


def mcp_tool_to_openai(tool):
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": strip_unsupported_schema_keywords(tool.inputSchema),
        },
    }


async def investigate(sessions, alert_text):
    """sessions: one MCP session or a list; tool schemas are merged and each call routed back to whichever session owns that tool name."""
    if not isinstance(sessions, (list, tuple)):
        sessions = [sessions]

    tools = []
    tool_owner = {}
    for session in sessions:
        tools_resp = await session.list_tools()
        for t in tools_resp.tools:
            tools.append(mcp_tool_to_openai(t))
            tool_owner[t.name] = session

    messages = [
        {"role": "user", "content": load_prompt("agentic_investigate.txt", alert_text=alert_text)}
    ]

    tool_call_count = 0
    seen_calls = set()
    for turn in range(MAX_TOOL_TURNS):
        try:
            message = chat_completion(messages, tools=tools)
        except urllib.error.HTTPError as exc:
            # The conversation grows every turn; deep enough in, llama.cpp 400s with exceed_context_size_error instead of responding.
            if exc.code == 400:
                return "(ran out of context budget partway through the investigation - could not reach a diagnosis)"
            raise
        messages.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            # Model sometimes narrates a fake tool call as text instead of using tool_calls, and max_tokens cuts it off mid-JSON.
            if message.get("finish_reason") == "length":
                return "(model response was truncated before completing a real answer - could not reach a diagnosis)"
            if tool_call_count < MIN_TOOL_CALLS and turn < MAX_TOOL_TURNS - 1:
                messages.append(
                    {
                        "role": "user",
                        "content": load_prompt(
                            "agentic_nudge.txt",
                            tool_call_count=tool_call_count,
                            min_tool_calls=MIN_TOOL_CALLS,
                        ),
                    }
                )
                continue
            return message.get("content") or "(model returned no diagnosis text)"

        tool_call_count += len(tool_calls)
        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            # Small models at temp=0 can get stuck repeating an identical failed call - refuse to re-run it.
            call_sig = (name, json.dumps(args, sort_keys=True))
            if call_sig in seen_calls:
                content = load_prompt("agentic_repeat_blocked.txt")
            elif name not in tool_owner:
                content = f"tool error: unknown tool {name!r}"
            else:
                seen_calls.add(call_sig)
                content = await call_tool_text(tool_owner[name], name, args)
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": content}
            )

    return "Investigation did not conclude within the tool-call budget."
