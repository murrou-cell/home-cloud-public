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
import asyncio
import ast
import base64
import difflib
import importlib.util
import os
import re
import sys
import textwrap

import anthropic
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import common
from common import K8S_MCP_URL


class ToolCallFailed(Exception):
    """Raised only during the dry run when the underlying MCP tool itself reports isError=True.

    common.call_tool_text() deliberately swallows this into descriptive text instead of raising -
    the right behavior in production (a graceful "couldn't check" answer for the model to describe),
    but it means a real RBAC/tool failure can slip past a dry run that only checks "did investigate()
    raise or return text" - the diagnosis LLM will happily paraphrase an error into plausible-sounding
    prose. PR #116 was exactly this: correct code, but the tool call was silently forbidden and the
    model wrote a coherent paragraph about it anyway. Patching call_tool_text for the dry run only
    (proposed modules pick this up via their own `from common import call_tool_text`, executed after
    this patch is already in place) closes that gap by checking the one field that unambiguously
    distinguishes a tool failure from a real answer.
    """


async def _strict_call_tool_text(session, name, args, max_chars=None):
    result = await session.call_tool(name, args)
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    if getattr(result, "isError", False):
        raise ToolCallFailed(f"MCP tool {name!r} reported isError=True: {text}")
    return text


common.call_tool_text = _strict_call_tool_text

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
PLAYBOOKS_INIT_PATH = f"{PLAYBOOKS_DIR}/__init__.py"
GATEWAY_PATH = "bridge/mcp-gateway/gateway.py"
INTENT_CLASSIFY_PATH = "bridge/mcp-gateway/prompts/intent_classify.txt"
# The exact line build_target() ends with - new /ask-only elif branches are spliced in immediately before it.
BUILD_TARGET_END_MARKER = "    return None, None"
# Matches an intent bullet line, e.g. `- "cronjob": ...` - used to find where to insert a new one and to extract intent names.
INTENT_BULLET_RE = re.compile(r'^- "([a-z_]+)":.*$', re.MULTILINE)
# Real production investigations complete well under a minute; this just bounds the dry run, not Claude's own turnaround.
DRY_RUN_TIMEOUT = 90

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


def extract_function_source(content, func_name):
    """Returns just one function's source text via its AST line range, so a huge file (gateway.py) can be shown as focused context instead of its full ~500 lines."""
    tree = ast.parse(content)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            lines = content.splitlines()
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    return None


PROPOSE_TOOL = {
    "name": "propose_playbook_change",
    "description": (
        "Propose a playbook code change, and - only for a brand-new playbook - the small "
        "wiring piece needed to register it, all opened as one PR for human review."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target_file": {
                "type": "string",
                "description": "Repo-relative path to the playbook module, e.g. bridge/mcp-gateway/playbooks/host.py",
            },
            "new_content": {
                "type": "string",
                "description": "The COMPLETE new content of target_file, not a diff or partial snippet.",
            },
            "commit_message": {"type": "string"},
            "is_new_playbook": {"type": "boolean"},
            "wiring": {
                "type": "object",
                "description": (
                    "Omit entirely unless is_new_playbook is true. Provide EITHER playbooks_list_module "
                    "alone (alert-shape), OR ask_branch_code + intent_classify_line together (/ask-only) "
                    "- never a mix, never for a fix to an already-wired playbook. The harness splices "
                    "these into the real files itself; you are not editing gateway.py, "
                    "playbooks/__init__.py, or the classifier prompt directly, so give only the small "
                    "pieces described."
                ),
                "properties": {
                    "playbooks_list_module": {
                        "type": "string",
                        "description": (
                            "Alert-shape playbooks only (extract_target reads payload['alerts']): the "
                            "bare module name to register in playbooks/__init__.py's PLAYBOOKS list - "
                            "must exactly equal target_file's module name, e.g. 'argo_workflow'."
                        ),
                    },
                    "ask_branch_code": {
                        "type": "string",
                        "description": (
                            "/ask-only playbooks only: the COMPLETE new elif branch to splice into "
                            "gateway.py's build_target(), matching the style of its existing branches "
                            "exactly, e.g.:\n"
                            '    elif intent == "workflow":\n'
                            '        name = intent_data.get("name")\n'
                            "        if name:\n"
                            "            return workflow, {\"name\": name}\n"
                            "Must start with 4 spaces then 'elif intent == \"<intent>\":'. The module "
                            "name used here (e.g. `workflow`) must exactly equal target_file's module "
                            "name - it will be added to gateway.py's existing playbooks import for you. "
                            "MUST be accompanied by intent_classify_line below - build_target() is only "
                            "ever reached for an intent the classifier itself already decided to emit."
                        ),
                    },
                    "intent_classify_line": {
                        "type": "string",
                        "description": (
                            "Required together with ask_branch_code, never with playbooks_list_module: one "
                            "COMPLETE new bullet line teaching the classifier this intent exists, in exactly "
                            "the same format as every other line in that list, e.g.:\n"
                            '- "workflow": a specific Argo Workflow\'s completion status - requires "name" '
                            "(the Workflow name)\n"
                            'Must start with `- "<intent>":` where <intent> is the exact same string used '
                            "in ask_branch_code's `elif intent == \"...\":`. Without this, the classifier "
                            "will never emit that intent and the new branch is unreachable - this was a "
                            "real bug (PR #113): a wired build_target() branch that nothing ever routed to."
                        ),
                    },
                },
            },
            "explanation": {
                "type": "string",
                "description": (
                    "2-4 sentences: what evidence-gathering step was missing or wrong, and "
                    "why this change fixes it. Shown verbatim in the PR description."
                ),
            },
            "dry_run_target": {
                "type": "object",
                "description": (
                    "Required: a realistic `target` dict to actually call this playbook's "
                    "investigate(session, alert_text, target) with, against a real read-only MCP "
                    "session, before any PR is opened - this is how the harness catches problems no "
                    "static check can see (e.g. missing RBAC, a wrong apiVersion/kind, a real tool "
                    "error). Never used for production dispatch, only this pre-merge check. Match the "
                    "target shape investigate() actually reads - e.g. {\"name\": \"...\"} or "
                    "{\"namespace\": \"...\", \"name\": \"...\"} - using a real name/namespace implied "
                    "by the alert/question above wherever possible."
                ),
            },
        },
        "required": [
            "target_file", "new_content", "commit_message", "is_new_playbook", "explanation", "dry_run_target",
        ],
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
Always return the COMPLETE new file content for target_file, never a diff or partial \
snippet. Do not invent MCP tool names that were not shown in the evidence. MCP tool \
names (e.g. resources_get) are string literals passed as the second argument to \
`common.call_tool_text(session, "tool_name", args)` - they are never imported from \
`common`, and `common` exports no function with the same name as any MCP tool. Do not \
change unrelated code in the file. If you cannot identify a concrete, justified \
improvement from what you're given, still call the tool: set new_content to the \
original file's content unchanged and explain in `explanation` that no safe change \
could be identified - a human reviewing a no-op PR is preferable to no signal at all \
that this alert stumped the pipeline.

When proposing a brand-new playbook, also fill in `wiring` so it's actually registered \
in the same PR instead of left as a manual follow-up - see that field's description for \
the exact shape. Use `playbooks_list_module` for an alert-shape playbook, or both \
`ask_branch_code` and `intent_classify_line` together for a /ask-only one - a /ask-only \
playbook needs both or the classifier will never route to it. You are not given \
gateway.py, playbooks/__init__.py, or the classifier prompt as files to rewrite \
wholesale - only supply the small pieces requested and the harness splices them in \
mechanically.

Always fill in `dry_run_target` too - the harness actually calls investigate() with it \
against a live read-only MCP session before opening any PR, which catches problems no \
static check can (e.g. missing RBAC on the resource kind, a wrong apiVersion/kind, a \
real tool error). Base it on a real name/namespace the alert/question implies wherever \
possible, so the check is meaningful rather than trivially satisfied."""


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
                f"invented name. Note it imports no MCP_URL constant at all - a playbook needs one ONLY when it "
                f"targets a non-default MCP server like Reference 1's PROXMOX_MCP_URL; the Kubernetes MCP session "
                f"is already the default, so importing anything like `KUBERNETES_MCP_URL` (not a real name) is "
                f"never necessary and will crash the import. If the new playbook inspects a Kubernetes object, "
                f"follow this pattern, not Reference 1's Proxmox-specific tools:\n```python\n{ref2_content}\n```\n"
            )
        if ALERTNAME == "ask":
            gateway_content, _ = fetch_file(GATEWAY_PATH)
            build_target_src = extract_function_source(gateway_content, "build_target") if gateway_content else None
            classify_content, _ = fetch_file(INTENT_CLASSIFY_PATH)
            parts.append(
                "This came from the /ask chat endpoint, not an Alertmanager alert - no intent in "
                "gateway.py's build_target() matched the question at all. Propose a new playbook "
                "module if one is clearly warranted, at bridge/mcp-gateway/playbooks/<name>.py, "
                "following the exact same extract_target()/investigate() interface as the reference "
                "above. This new module must NEVER be registered in playbooks/__init__.py's PLAYBOOKS "
                "list - that registry is for Alertmanager alert dispatch only, checked in order, "
                "first-match-wins; an unconditionally-matching extract_target() there would silently "
                "break the alert path's fallback to the agentic loop for every alert shape no other "
                "playbook claims. Instead, fill in `wiring.ask_branch_code` with a new elif branch "
                "matching the style of build_target()'s existing branches exactly:\n"
                f"```python\n{build_target_src}\n```\n"
            )
            if classify_content:
                parts.append(
                    "The classifier prompt below decides which intent string build_target() ever sees - "
                    "a branch for an intent this prompt doesn't mention is unreachable dead code (this "
                    "exact bug shipped once). You MUST also fill in `wiring.intent_classify_line` with a "
                    "new bullet line in the exact same format as the existing ones, using the identical "
                    "intent name as your ask_branch_code:\n"
                    f"```\n{classify_content}\n```\n"
                )
        else:
            parts.append(
                "No existing playbook claimed this alert shape (it went through the open-ended "
                "agentic fallback instead). Propose a new playbook module if one is clearly "
                "warranted, at bridge/mcp-gateway/playbooks/<name>.py, following the exact same "
                "interface as the reference above, and fill in `wiring.playbooks_list_module` with "
                "its bare module name so it's registered in the same PR.\n"
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


COMMON_MODULE_PATH = "bridge/mcp-gateway/common.py"


def fetch_common_module_names():
    """Derives what common.py actually exports (top-level defs/classes/assignments) via the GitHub API, the same live-derivation approach as fetch_known_tool_names - catches PR #115's `from common import KUBERNETES_MCP_URL` (the real name is K8S_MCP_URL) the same way a hallucinated tool name is caught."""
    content, _ = fetch_file(COMMON_MODULE_PATH)
    if not content:
        return set()
    tree = ast.parse(content)
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def extract_common_imports(source):
    """Returns the set of names a playbook imports via `from common import ...`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "common":
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def validate_proposal(new_content, allowed_tool_names, common_names):
    """Structural sanity check before ever opening a PR - returns (ok, reason). Catches PR #107's hallucinated session.llm() call, PR #110's hallucinated get_resource tool name, and PR #115's hallucinated KUBERNETES_MCP_URL import, plus syntax errors and a missing investigate().

    Collects every problem found in one pass rather than stopping at the first - the harness only allows one retry, and a proposal has been seen to carry two independent, unrelated bugs at once (e.g. a bad tool name AND a bad import together); reporting just the first meant the retry fixed one and never even saw the other."""
    try:
        tree = ast.parse(new_content)
    except SyntaxError as exc:
        return False, f"proposed file is not valid Python: {exc}"

    problems = []

    if not any(isinstance(n, ast.AsyncFunctionDef) and n.name == "investigate" for n in ast.walk(tree)):
        problems.append("missing an `async def investigate(session, alert_text, target)` function")

    bad_session_methods = set()
    for node in ast.walk(tree):
        is_session_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "session"
        )
        if is_session_call and node.func.attr not in ALLOWED_SESSION_METHODS:
            bad_session_methods.add(node.func.attr)
    if bad_session_methods:
        problems.append(
            f"calls session.{{{', '.join(sorted(bad_session_methods))}}}(...), which {'are' if len(bad_session_methods) > 1 else 'is'} "
            f"not real MCP ClientSession method(s) (only {sorted(ALLOWED_SESSION_METHODS)} exist)"
        )

    unknown_tools = extract_tool_names(new_content) - allowed_tool_names
    if unknown_tools:
        problems.append(
            f"calls MCP tool(s) {sorted(unknown_tools)}, which are not used anywhere in the existing playbooks "
            f"(known real tools: {sorted(allowed_tool_names)}) - this looks like an invented tool name"
        )

    unknown_common_imports = extract_common_imports(new_content) - common_names
    if unknown_common_imports:
        confused_with_tools = unknown_common_imports & allowed_tool_names
        if confused_with_tools:
            plural = len(confused_with_tools) > 1
            problems.append(
                f"imports {sorted(confused_with_tools)} from `common` - but {'these are real MCP tool names' if plural else 'this is a real MCP tool name'}, "
                f"not a Python symbol to import. Tool names are never imported; they're string literals passed as "
                f"the second argument to `call_tool_text(session, \"{sorted(confused_with_tools)[0]}\", args)`"
            )
        other_unknown = unknown_common_imports - allowed_tool_names
        if other_unknown:
            problems.append(
                f"imports {sorted(other_unknown)} from `common`, which {'do' if len(other_unknown) > 1 else 'does'} "
                f"not exist there - this looks like a hallucinated name (e.g. K8S_MCP_URL is the real k8s MCP URL constant, "
                f"not KUBERNETES_MCP_URL); a playbook needs no MCP_URL attribute at all to use the default k8s session"
            )

    if problems:
        return False, "; ".join(problems)
    return True, None


def is_additive_only(old_content, new_content):
    """True only if new_content preserves every line of old_content unchanged and in order, adding lines but never deleting or rewriting one. The safety property that lets wiring edits touch gateway.py/__init__.py without an LLM regenerating the whole file."""
    sm = difflib.SequenceMatcher(a=old_content.splitlines(), b=new_content.splitlines(), autojunk=False)
    return all(tag in ("equal", "insert") for tag, *_ in sm.get_opcodes())


def add_import_line(content, import_prefix, module_name):
    """Inserts a new standalone `<import_prefix><module_name>` line right after the existing import line with that prefix, rather than rewriting that line in place - keeps the change a pure line insertion so is_additive_only holds."""
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if stripped.startswith(import_prefix):
            existing_names = [n.strip() for n in stripped[len(import_prefix) :].split(",")]
            if module_name in existing_names:
                return content
            new_line = f"{import_prefix}{module_name}\n"
            return "".join(lines[: i + 1]) + new_line + "".join(lines[i + 1 :])
    raise ValueError(f"import line starting with {import_prefix!r} not found")


def playbooks_list_names(content):
    """Extracts the bare module names in playbooks/__init__.py's `PLAYBOOKS = [...]` list literal via AST, or None if not found."""
    tree = ast.parse(content)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLAYBOOKS" for t in node.targets):
            if isinstance(node.value, ast.List):
                return [elt.id for elt in node.value.elts if isinstance(elt, ast.Name)]
    return None


def is_additive_except_playbooks_line(old_content, new_content):
    """Same guarantee as is_additive_only for every line except PLAYBOOKS = [...] itself, which is the one line this wiring is allowed to rewrite in place (checked separately via playbooks_list_names)."""
    strip = lambda text: "\n".join(l for l in text.splitlines() if not l.startswith("PLAYBOOKS = ["))
    return is_additive_only(strip(old_content), strip(new_content))


def infer_module_name(ask_branch_code):
    """The tool schema doesn't carry the module name for the ask-branch case directly, so pull it from `return <module>, {...}` in the proposed branch."""
    m = re.search(r"return\s+([A-Za-z_][A-Za-z0-9_]*)\s*,", ask_branch_code)
    if not m:
        raise ValueError("could not find `return <module>, {...}` in ask_branch_code to determine the module name")
    return m.group(1)


def infer_intent_name(ask_branch_code):
    """Pulls the intent string out of `elif intent == "<intent>":` so it can be cross-checked against intent_classify_line."""
    m = re.search(r'elif\s+intent\s*==\s*"([a-z_]+)"\s*:', ask_branch_code)
    if not m:
        raise ValueError('could not find `elif intent == "..."` in ask_branch_code to determine the intent name')
    return m.group(1)


def prepare_intent_classify_wiring(intent_name, line_text):
    """Mechanically splices a new intent bullet line into intent_classify.txt rather than asking Claude to regenerate the whole prompt file. Returns (original_content, new_content, existing_sha) or raises ValueError."""
    line_text = line_text.strip()
    if "\n" in line_text:
        raise ValueError("intent_classify_line must be a single line")
    m = re.match(r'^- "([a-z_]+)":', line_text)
    if not m:
        raise ValueError('intent_classify_line must start with \'- "<intent>":\'')
    if m.group(1) != intent_name:
        raise ValueError(
            f"intent_classify_line declares intent `{m.group(1)}`, but ask_branch_code's elif checks "
            f"intent `{intent_name}` - they must match"
        )

    original, sha = fetch_file(INTENT_CLASSIFY_PATH)
    existing = {bm.group(1) for bm in INTENT_BULLET_RE.finditer(original)}
    if intent_name in existing:
        raise ValueError(f"intent `{intent_name}` is already listed in intent_classify.txt")

    bullets = list(INTENT_BULLET_RE.finditer(original))
    if not bullets:
        raise ValueError("no existing intent bullet lines found in intent_classify.txt to insert after")
    insert_at = bullets[-1].end()
    new_content = original[:insert_at] + "\n" + line_text + original[insert_at:]
    return original, new_content, sha


def normalize_branch_indent(branch_code):
    """Dedents branch_code to its own minimal common indentation, then re-indents every line by exactly 4 spaces - so it lands at build_target()'s real indentation level regardless of whatever absolute indentation Claude used, as long as it's internally consistent."""
    lines = [l for l in branch_code.splitlines() if l.strip() != ""]
    dedented = textwrap.dedent("\n".join(lines))
    return "\n".join(("    " + l) if l else l for l in dedented.splitlines())


def prepare_ask_branch_wiring(module_name, branch_code):
    """Mechanically splices a new import + elif branch into gateway.py rather than asking Claude to regenerate the whole ~500-line file. Returns (original_content, new_content, existing_sha) or raises ValueError."""
    branch_code = normalize_branch_indent(branch_code)
    if not branch_code.startswith('    elif intent == "'):
        raise ValueError('ask_branch_code must be a single elif intent == "..." branch')
    original, sha = fetch_file(GATEWAY_PATH)
    content = add_import_line(original, "from playbooks import ", module_name)
    if content.count(BUILD_TARGET_END_MARKER) != 1:
        raise ValueError("could not find build_target()'s unique closing return line to splice before")
    branch_text = branch_code.rstrip("\n") + "\n\n"
    new_content = content.replace(BUILD_TARGET_END_MARKER, branch_text + BUILD_TARGET_END_MARKER, 1)
    return original, new_content, sha


def prepare_playbooks_list_wiring(module_name):
    """Mechanically splices a new import + PLAYBOOKS entry into playbooks/__init__.py rather than asking Claude to regenerate it. Returns (original_content, new_content, existing_sha) or raises ValueError."""
    original, sha = fetch_file(PLAYBOOKS_INIT_PATH)
    content = add_import_line(original, "from . import ", module_name)
    m = re.search(r"^PLAYBOOKS = \[(.*)\]$", content, re.MULTILINE)
    if not m:
        raise ValueError("PLAYBOOKS = [...] line not found in playbooks/__init__.py")
    names = [n.strip() for n in m.group(1).split(",") if n.strip()]
    if module_name not in names:
        names.append(module_name)
    new_content = content[: m.start()] + f"PLAYBOOKS = [{', '.join(names)}]" + content[m.end() :]
    return original, new_content, sha


def validate_file_wiring(path, original, new_content, *, playbooks_list_module=None):
    """Shared post-splice checks for one wiring file: valid Python (skipped for the plain-text classifier prompt) plus the appropriate additive-safety check. Returns (ok, reason)."""
    if path != INTENT_CLASSIFY_PATH:
        try:
            ast.parse(new_content)
        except SyntaxError as exc:
            return False, f"wiring produced invalid Python in {path}: {exc}"

    if path == PLAYBOOKS_INIT_PATH:
        old_names, new_names = playbooks_list_names(original), playbooks_list_names(new_content)
        if old_names is None or new_names is None:
            return False, "could not find a PLAYBOOKS = [...] list literal in playbooks/__init__.py"
        if new_names[: len(old_names)] != old_names or set(new_names) - set(old_names) != {playbooks_list_module}:
            return False, "wiring did not cleanly append exactly the new module to PLAYBOOKS, without disturbing the existing entries"
        if not is_additive_except_playbooks_line(original, new_content):
            return False, "wiring changed something in __init__.py besides the import and the PLAYBOOKS line"
    else:
        if not is_additive_only(original, new_content):
            return False, f"wiring would modify or remove existing lines in {path}, not just add to it"

    return True, None


def validate_wiring(candidate):
    """Applies whichever wiring the proposal requested and validates the result (syntax + additive-only). Returns (ok, reason, extra_files) where extra_files is a list of (path, new_content, existing_sha) to commit alongside the playbook file."""
    wiring = candidate.get("wiring")
    if not wiring:
        return True, None, []

    playbooks_list_module = wiring.get("playbooks_list_module")
    ask_branch_code = wiring.get("ask_branch_code")
    intent_classify_line = wiring.get("intent_classify_line")
    if playbooks_list_module and ask_branch_code:
        return False, "wiring must set only one of playbooks_list_module or ask_branch_code, not both", []
    if playbooks_list_module and intent_classify_line:
        return False, "intent_classify_line only applies to ask_branch_code, never to playbooks_list_module", []
    if ask_branch_code and not intent_classify_line:
        return False, "ask_branch_code requires intent_classify_line too, or the classifier will never route to it", []

    files = []
    try:
        if playbooks_list_module:
            expected_module_name = candidate["target_file"].rsplit("/", 1)[-1].removesuffix(".py")
            if playbooks_list_module != expected_module_name:
                return False, (
                    f"wiring.playbooks_list_module is `{playbooks_list_module}`, but target_file's module name "
                    f"is `{expected_module_name}` - they must match"
                ), []
            original, new_content, sha = prepare_playbooks_list_wiring(playbooks_list_module)
            files.append((PLAYBOOKS_INIT_PATH, original, new_content, sha))
        elif ask_branch_code:
            module_name = infer_module_name(ask_branch_code)
            expected_module_name = candidate["target_file"].rsplit("/", 1)[-1].removesuffix(".py")
            if module_name != expected_module_name:
                return False, (
                    f"ask_branch_code returns module `{module_name}`, but target_file's module name is "
                    f"`{expected_module_name}` - they must match"
                ), []
            intent_name = infer_intent_name(ask_branch_code)
            original, new_content, sha = prepare_ask_branch_wiring(module_name, ask_branch_code)
            files.append((GATEWAY_PATH, original, new_content, sha))
            original2, new_content2, sha2 = prepare_intent_classify_wiring(intent_name, intent_classify_line)
            files.append((INTENT_CLASSIFY_PATH, original2, new_content2, sha2))
        else:
            return True, None, []
    except ValueError as exc:
        return False, f"wiring failed: {exc}", []

    for path, original, new_content, sha in files:
        ok, reason = validate_file_wiring(path, original, new_content, playbooks_list_module=playbooks_list_module)
        if not ok:
            return False, reason, []

    return True, None, [(path, new_content, sha) for path, _, new_content, sha in files]


def load_module_from_source(source, module_name="proposed_playbook"):
    """Executes proposed playbook source as a real module object so investigate() can be called directly - the same interface production code uses, not a reimplementation of it."""
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, f"<{module_name}>", "exec"), module.__dict__)  # noqa: S102 - the whole point is to run the proposed code for real, against read-only tools only
    return module


async def _dry_run_investigate_async(module, alert_text, target):
    mcp_url = getattr(module, "MCP_URL", K8S_MCP_URL)
    async with streamablehttp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await asyncio.wait_for(module.investigate(session, alert_text, target), timeout=DRY_RUN_TIMEOUT)


def dry_run_investigate(new_content, alert_text, dry_run_target):
    """Actually calls the proposed investigate() against a live read-only MCP session, using the same question/alert that triggered this escalation. Returns (ok, reason). This is the only check in the whole harness that can catch a runtime-only failure (missing RBAC, a wrong apiVersion/kind, a real tool error) - PR #116 was structurally perfect and still failed this way on the live cluster."""
    try:
        module = load_module_from_source(new_content)
    except Exception as exc:  # noqa: BLE001 - any failure to even import the proposed module is a real problem to report
        return False, f"dry run: executing the proposed module raised {exc!r}"

    if not hasattr(module, "investigate"):
        return False, "dry run: proposed module has no investigate() to call"

    try:
        result = asyncio.run(_dry_run_investigate_async(module, alert_text, dry_run_target or {}))
    except Exception as exc:  # noqa: BLE001 - surfaced as a validation failure, not a crash of the harness itself
        return False, (
            f"dry run: investigate() raised {exc!r} against a real read-only MCP session with target "
            f"{dry_run_target!r} - only a live call catches this class of problem (e.g. missing RBAC, "
            f"wrong apiVersion/kind, a real tool error)"
        )

    if not isinstance(result, str) or not result.strip():
        return False, "dry run: investigate() ran without raising but returned no usable text"

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
    common_names = fetch_common_module_names()

    proposal = None
    extra_files = []
    reason = None
    for attempt in range(2):
        tool_use, response = request_proposal(client, messages)
        if tool_use is None:
            print("Claude did not return a tool call - nothing to open a PR with.", file=sys.stderr)
            sys.exit(1)
        candidate = tool_use.input
        ok, reason = validate_proposal(candidate["new_content"], allowed_tool_names, common_names)
        if ok:
            ok, reason, extra_files = validate_wiring(candidate)
        if ok:
            print("stage: dry-running investigate() against a live MCP session")
            ok, reason = dry_run_investigate(candidate["new_content"], ALERT_TEXT, candidate.get("dry_run_target"))
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
                    "content": f"Validation failed: {reason}. Call the tool again with a corrected proposal.",
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

    all_files = [(target_file, new_content, existing_sha)] + extra_files
    for path, content, sha in all_files:
        put_body = {
            "message": commit_message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            put_body["sha"] = sha
        gh_put(f"/repos/{REPO}/contents/{path}", put_body)

    files_list = "\n".join(f"- `{path}`" for path, _, _ in all_files)
    pr_body = (
        "Opened automatically by the Claude-escalation Workflow "
        "(argocd/apps/claude-playbook-workflows/) after the local pipeline escalated "
        "an alert it couldn't confidently diagnose.\n\n"
        f"**Files changed:**\n{files_list}\n\n"
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
