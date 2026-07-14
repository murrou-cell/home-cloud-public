"""Open-ended tool-use loop against llama.cpp's OpenAI-compatible function
calling, driving the kubernetes-mcp-server tools directly. The fallback for
any alert no playbook in playbooks/ claims - there's no fixed
evidence-gathering sequence to hand the model for those, so it has to plan
its own investigation, which it does less reliably than following a fixed
sequence (see playbooks/pod.py's docstring)."""
import json

from common import call_tool_text, chat_completion, load_prompt

# Bumped from 8: the repeat-call guard below makes a blocked repeat cost a
# turn without producing new evidence, so a run that needs a couple of
# self-corrections needs headroom beyond the old budget.
MAX_TOOL_TURNS = 12
# Qwen2.5-3B reliably calls a tool or two, then concludes with a guess and an
# offer like "provide the YAML if you need further help" - there's no human
# to ask, so force it to spend a couple more tool calls verifying instead of
# guessing from a single log line.
MIN_TOOL_CALLS = 3


def strip_unsupported_schema_keywords(schema):
    """llama.cpp's JSON-schema-to-GBNF grammar converter can't parse the
    "pattern" keyword (regex) and fails the whole request with a 400 -
    confirmed by testing each kubernetes-mcp-server tool individually,
    100% correlated with which ones declare a "pattern". Strip it
    recursively; it's a validation hint, not something the model needs
    in order to call the tool."""
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


async def investigate(session, alert_text):
    tools_resp = await session.list_tools()
    tools = [mcp_tool_to_openai(t) for t in tools_resp.tools]

    messages = [
        {"role": "user", "content": load_prompt("agentic_investigate.txt", alert_text=alert_text)}
    ]

    tool_call_count = 0
    seen_calls = set()
    for turn in range(MAX_TOOL_TURNS):
        message = chat_completion(messages, tools=tools)
        messages.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
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
            # Small models at temp=0 can get stuck repeating an identical failed
            # call instead of adapting to evidence already in context - refuse to
            # re-run it and push back instead of burning the tool-turn budget.
            call_sig = (name, json.dumps(args, sort_keys=True))
            if call_sig in seen_calls:
                content = load_prompt("agentic_repeat_blocked.txt")
            else:
                seen_calls.add(call_sig)
                content = await call_tool_text(session, name, args)
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": content}
            )

    return "Investigation did not conclude within the tool-call budget."
