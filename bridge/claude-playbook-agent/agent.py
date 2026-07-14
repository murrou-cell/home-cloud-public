#!/usr/bin/env python3
"""Runs as one step of the claude-improve-playbook Argo Workflow
(argocd/apps/claude-playbook-workflows/), triggered by mcp-gateway when the
local llama.cpp pipeline escalates an alert it couldn't confidently
diagnose (see gateway.py's maybe_escalate_to_claude). Calls the real Claude
API to propose a concrete improvement to the playbook that handled - or
should have handled - the alert, and opens it as a PR against home-cloud
for human review. Nothing here ever pushes to main or auto-merges.

Only runs at all when mcp-gateway's CLAUDE_ESCALATION_ENABLED kill switch
(argocd/apps/mcp-gateway/values.yaml) is flipped on, which requires real
Anthropic API billing to exist - see that values.yaml comment.
"""
import ast
import base64
import os
import sys

import anthropic
import httpx

REPO = os.environ.get("GITHUB_REPO", "murrou-cell/home-cloud")
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# Low volume (a handful of escalations/week at most), high stakes (a wrong
# code change is worse than no change) - cost is not a reason to downgrade
# the model here.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

ALERTNAME = os.environ.get("ALERTNAME", "alert")
ALERT_TEXT = os.environ.get("ALERT_TEXT", "")
DIAGNOSIS = os.environ.get("DIAGNOSIS", "")
TARGET_FILE = os.environ.get("TARGET_FILE") or None
WORKFLOW_NAME = os.environ.get("WORKFLOW_NAME", "run")

# Concrete examples for new-playbook proposals - without one, Claude invented a nonexistent session.llm() helper.
# Two references, not one: proxmox.py shows the /ask-only pattern against a non-Kubernetes MCP server; argocd.py
# shows the generic Kubernetes-CR-reading pattern (resources_get/resources_list/events_list) most new Kubernetes
# playbooks actually need - a single reference previously left Claude to invent a plausible-but-fake tool name
# (get_resource instead of the real resources_get) when the question needed this second pattern.
REFERENCE_PLAYBOOK_PATH = "bridge/mcp-gateway/playbooks/proxmox.py"
SECOND_REFERENCE_PLAYBOOK_PATH = "bridge/mcp-gateway/playbooks/argocd.py"
PLAYBOOKS_DIR = "bridge/mcp-gateway/playbooks"

GITHUB_API = "https://api.github.com"
_gh_headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def gh_get(path, **params):
    resp = httpx.get(f"{GITHUB_API}{path}", headers=_gh_headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def gh_post(path, body):
    resp = httpx.post(f"{GITHUB_API}{path}", headers=_gh_headers, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def gh_put(path, body):
    resp = httpx.put(f"{GITHUB_API}{path}", headers=_gh_headers, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_file(path, ref="main"):
    """Returns (content_text, sha), or (None, None) if the file doesn't exist yet."""
    try:
        data = gh_get(f"/repos/{REPO}/contents/{path}", ref=ref)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None, None
        raise
    return base64.b64decode(data["content"]).decode(), data["sha"]


PROPOSE_TOOL = {
    "name": "propose_playbook_change",
    "description": (
        "Propose a concrete change to this project's alert-diagnosis playbook code, "
        "to be opened as a PR for human review."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target_file": {
                "type": "string",
                "description": "Repo-relative path, e.g. bridge/mcp-gateway/playbooks/host.py",
            },
            "new_content": {
                "type": "string",
                "description": "The COMPLETE new file content, not a diff or partial snippet.",
            },
            "commit_message": {"type": "string"},
            "is_new_playbook": {"type": "boolean"},
            "explanation": {
                "type": "string",
                "description": (
                    "2-4 sentences: what evidence-gathering step was missing or wrong, and "
                    "why this change fixes it. Shown verbatim in the PR description."
                ),
            },
        },
        "required": ["target_file", "new_content", "commit_message", "is_new_playbook", "explanation"],
    },
}

SYSTEM_PROMPT = """\
You improve a Kubernetes alert-diagnosis pipeline's playbook code. A playbook is a \
Python module with extract_target(payload) and async investigate(session, alert_text, \
target) functions - it gathers evidence deterministically via MCP tool calls, then \
makes exactly one LLM call to turn that evidence into prose. The pipeline's local \
model (a 3B-parameter model) just escalated a case it couldn't resolve. You're given \
the alert, the low-confidence result it produced, and - if one exists - the current \
source of the playbook that handled it.

Propose ONE concrete, minimal code change: either a fix to the existing playbook \
(e.g. it's missing an evidence-gathering step, mis-parses a field, or its \
target-matching logic misses this alert shape), or - only if no playbook exists for \
this alert shape at all - a new playbook module following the exact same interface. \
Always return the COMPLETE new file content, never a diff or partial snippet. Do not \
invent MCP tool names that were not shown in the evidence. Do not change unrelated \
code in the file. If you cannot identify a concrete, justified improvement from what \
you're given, still call the tool: set new_content to the original file's content \
unchanged and explain in `explanation` that no safe change could be identified - a \
human reviewing a no-op PR is preferable to no signal at all that this alert stumped \
the pipeline."""


def build_user_message():
    parts = [f"Alert:\n{ALERT_TEXT}\n"]
    parts.append(f"Result the local model produced (low-confidence):\n{DIAGNOSIS}\n")
    if TARGET_FILE:
        content, _ = fetch_file(TARGET_FILE)
        if content:
            parts.append(f"Current contents of {TARGET_FILE}:\n```python\n{content}\n```\n")
        else:
            parts.append(f"{TARGET_FILE} does not exist yet - propose it as a new playbook.\n")
    else:
        ref_content, _ = fetch_file(REFERENCE_PLAYBOOK_PATH)
        if ref_content:
            parts.append(
                f"Reference 1 - an existing real playbook ({REFERENCE_PLAYBOOK_PATH}), showing the exact interface "
                f"and helper functions to use. Any new playbook MUST use `chat_completion()` and `load_prompt()` "
                f"imported from `common` exactly like this one does - do not invent a different helper (e.g. there "
                f"is no `session.llm()` or similar; `session` is only ever used via `common.call_tool_text(session, "
                f"name, args)`):\n```python\n{ref_content}\n```\n"
            )
        ref2_content, _ = fetch_file(SECOND_REFERENCE_PLAYBOOK_PATH)
        if ref2_content:
            parts.append(
                f"Reference 2 - {SECOND_REFERENCE_PLAYBOOK_PATH}, showing the pattern most new Kubernetes-resource "
                f"playbooks need: reading an arbitrary Kubernetes object (including custom resources) via the real "
                f"generic tools `resources_get`/`resources_list`/`events_list` - NOT `get_resource` or any other "
                f"invented name. If the new playbook inspects a Kubernetes object, follow this pattern, not "
                f"Reference 1's Proxmox-specific tools:\n```python\n{ref2_content}\n```\n"
            )
        if ALERTNAME == "ask":
            parts.append(
                "This came from the /ask chat endpoint, not an Alertmanager alert - no intent in "
                "gateway.py's build_target() matched the question at all. Propose a new playbook "
                "module if one is clearly warranted, at bridge/mcp-gateway/playbooks/<name>.py, "
                "following the exact same extract_target()/investigate() interface as the reference "
                "above. You can only change one file in this PR (target_file) - if wiring the new "
                "playbook into gateway.py's build_target() would also be needed, say so explicitly "
                "in `explanation` as a required follow-up, don't silently omit it. This new module "
                "must NEVER be added to playbooks/__init__.py's PLAYBOOKS list - that registry is for "
                "Alertmanager alert dispatch only, checked in order, first-match-wins; an "
                "unconditionally-matching extract_target() there would silently break the alert "
                "path's fallback to the agentic loop for every alert shape no other playbook claims. "
                "Wiring for a /ask-only playbook belongs solely in gateway.py's build_target().\n"
            )
        else:
            parts.append(
                "No existing playbook claimed this alert shape (it went through the open-ended "
                "agentic fallback instead). Propose a new playbook module if one is clearly "
                "warranted, at bridge/mcp-gateway/playbooks/<name>.py, following the exact same "
                "interface as the reference above, and say in `explanation` that it also needs "
                "adding to playbooks/__init__.py's PLAYBOOKS list as a required follow-up (you can "
                "only change one file in this PR).\n"
            )
    return "\n".join(parts)


ALLOWED_SESSION_METHODS = {"call_tool", "list_tools"}


def extract_tool_names(source):
    """AST-walks a playbook source for call_tool_text(session, "name", ...) / session.call_tool("name", ...) calls, returning the literal tool-name strings passed. Used both to build the real allowlist from existing playbooks and to check a new proposal against it."""
    names = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_call_tool_text = isinstance(func, ast.Name) and func.id == "call_tool_text"
        is_session_call_tool = (
            isinstance(func, ast.Attribute)
            and func.attr == "call_tool"
            and isinstance(func.value, ast.Name)
            and func.value.id == "session"
        )
        if is_call_tool_text and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            names.add(node.args[1].value)
        elif is_session_call_tool and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            names.add(node.args[0].value)
    return names


def fetch_known_tool_names():
    """Derives the real MCP tool-name allowlist by scanning every existing playbook file via the GitHub API, rather than hardcoding a list here that would silently drift out of sync with the real codebase."""
    listing = gh_get(f"/repos/{REPO}/contents/{PLAYBOOKS_DIR}")
    names = set()
    for entry in listing:
        if entry.get("type") != "file" or not entry["name"].endswith(".py") or entry["name"] == "__init__.py":
            continue
        content, _ = fetch_file(f"{PLAYBOOKS_DIR}/{entry['name']}")
        if content:
            names |= extract_tool_names(content)
    return names


def validate_proposal(new_content, allowed_tool_names):
    """Structural sanity check before ever opening a PR - returns (ok, reason). Catches PR #107's hallucinated session.llm() call and PR #110's hallucinated get_resource tool name, plus syntax errors and a missing investigate()."""
    try:
        tree = ast.parse(new_content)
    except SyntaxError as exc:
        return False, f"proposed file is not valid Python: {exc}"

    if not any(isinstance(n, ast.AsyncFunctionDef) and n.name == "investigate" for n in ast.walk(tree)):
        return False, "missing an `async def investigate(session, alert_text, target)` function"

    for node in ast.walk(tree):
        is_session_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "session"
        )
        if is_session_call and node.func.attr not in ALLOWED_SESSION_METHODS:
            return False, (
                f"calls session.{node.func.attr}(...), which is not a real MCP ClientSession method "
                f"(only {sorted(ALLOWED_SESSION_METHODS)} exist)"
            )

    unknown_tools = extract_tool_names(new_content) - allowed_tool_names
    if unknown_tools:
        return False, (
            f"calls MCP tool(s) {sorted(unknown_tools)}, which are not used anywhere in the existing playbooks "
            f"(known real tools: {sorted(allowed_tool_names)}) - this looks like an invented tool name"
        )

    return True, None


def request_proposal(client, messages):
    """Returns (tool_use, response), or (None, response) if Claude didn't call the tool."""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        tools=[PROPOSE_TOOL],
        tool_choice={"type": "tool", "name": "propose_playbook_change"},
        messages=messages,
    )
    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    return tool_use, response


def main():
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    messages = [{"role": "user", "content": build_user_message()}]
    allowed_tool_names = fetch_known_tool_names()

    proposal = None
    reason = None
    for attempt in range(2):
        tool_use, response = request_proposal(client, messages)
        if tool_use is None:
            print("Claude did not return a tool call - nothing to open a PR with.", file=sys.stderr)
            sys.exit(1)
        candidate = tool_use.input
        ok, reason = validate_proposal(candidate["new_content"], allowed_tool_names)
        if ok:
            proposal = candidate
            break
        print(f"stage: validation failed (attempt {attempt}): {reason}", file=sys.stderr)
        if attempt == 0:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": f"Validation failed: {reason}. Call the tool again with a corrected new_content.",
                }],
            })

    if proposal is None:
        print(f"Claude's proposal failed validation twice, giving up - no PR opened: {reason}", file=sys.stderr)
        sys.exit(1)

    target_file = proposal["target_file"]
    new_content = proposal["new_content"]
    commit_message = proposal["commit_message"]
    explanation = proposal["explanation"]

    _, existing_sha = fetch_file(target_file)

    main_ref = gh_get(f"/repos/{REPO}/git/ref/heads/main")
    base_sha = main_ref["object"]["sha"]

    branch = f"claude/improve-playbook-{ALERTNAME.lower()}-{WORKFLOW_NAME}"
    gh_post(f"/repos/{REPO}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})

    put_body = {
        "message": commit_message,
        "content": base64.b64encode(new_content.encode()).decode(),
        "branch": branch,
    }
    if existing_sha:
        put_body["sha"] = existing_sha
    gh_put(f"/repos/{REPO}/contents/{target_file}", put_body)

    pr_body = (
        "Opened automatically by the Claude-escalation Workflow "
        "(argocd/apps/claude-playbook-workflows/) after the local pipeline escalated "
        "an alert it couldn't confidently diagnose.\n\n"
        f"**Alert:**\n```\n{ALERT_TEXT}\n```\n\n"
        f"**Why this change:**\n{explanation}\n\n"
        "Nothing here is auto-merged - review the diff before merging."
    )
    pr = gh_post(
        f"/repos/{REPO}/pulls",
        {
            "title": f"Claude: {commit_message}",
            "head": branch,
            "base": "main",
            "body": pr_body,
        },
    )
    print(f"Opened PR: {pr['html_url']}")


if __name__ == "__main__":
    main()
