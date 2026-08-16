"""Builtin tools.

list_dir/read_file/write_file are plain filesystem ops jailed through
server.workspace. run_python is the gateway into the sandbox. propose_tool
is the *only* way new capabilities enter the system, and it never executes
anything — see server/admin.py for the human approval step that does.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from server import sandbox, workspace
from server.tools.registry import ToolContext, ToolSpec

MAX_READ_BYTES = 200_000
MAX_OUTPUT_CHARS = 20_000

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def _list_dir(tool_input: dict, ctx: ToolContext) -> str:
    rel = tool_input.get("path", ".")
    try:
        target = workspace.resolve_safe_path(ctx.workspace_root, rel, must_exist=True)
    except (workspace.PathEscapeError, FileNotFoundError) as exc:
        return json.dumps({"error": str(exc)})
    if not target.is_dir():
        return json.dumps({"error": f"{rel!r} is not a directory"})
    entries = [
        {
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
            "size": child.stat().st_size if child.is_file() else None,
        }
        for child in sorted(target.iterdir())
    ]
    return json.dumps({"path": rel, "entries": entries})


def _read_file(tool_input: dict, ctx: ToolContext) -> str:
    rel = tool_input.get("path")
    if not rel:
        return json.dumps({"error": "path is required"})
    try:
        target = workspace.resolve_safe_path(ctx.workspace_root, rel, must_exist=True)
    except (workspace.PathEscapeError, FileNotFoundError) as exc:
        return json.dumps({"error": str(exc)})
    if not target.is_file():
        return json.dumps({"error": f"{rel!r} is not a file"})
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        return json.dumps({"error": f"{rel!r} is {size} bytes, over the {MAX_READ_BYTES} limit"})
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return json.dumps({"error": f"{rel!r} is not valid UTF-8 text (binary file?)"})
    return json.dumps({"path": rel, "content": content})


def _write_file(tool_input: dict, ctx: ToolContext) -> str:
    rel = tool_input.get("path")
    content = tool_input.get("content", "")
    if not rel:
        return json.dumps({"error": "path is required"})
    try:
        target = workspace.resolve_safe_path(ctx.workspace_root, rel)
    except workspace.PathEscapeError as exc:
        return json.dumps({"error": str(exc)})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return json.dumps({"path": rel, "bytes_written": len(content.encode("utf-8"))})


def _run_python(tool_input: dict, ctx: ToolContext) -> str:
    rel = tool_input.get("path")
    if not rel:
        return json.dumps({"error": "path is required"})
    args = tool_input.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return json.dumps({"error": "args must be a list of strings"})
    try:
        workspace.resolve_safe_path(ctx.workspace_root, rel, must_exist=True)
    except (workspace.PathEscapeError, FileNotFoundError) as exc:
        return json.dumps({"error": str(exc)})

    try:
        result = sandbox.run_python(
            ctx.config.sandbox, ctx.workspace_root, rel, args, network_mode=ctx.network_mode
        )
    except sandbox.SandboxError as exc:
        return json.dumps({"error": str(exc)})

    return json.dumps({
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "stdout": result.stdout[-MAX_OUTPUT_CHARS:],
        "stderr": result.stderr[-MAX_OUTPUT_CHARS:],
        "network_mode": ctx.network_mode,
    })


def _propose_tool(tool_input: dict, ctx: ToolContext) -> str:
    name = tool_input.get("name", "")
    description = tool_input.get("description", "")
    input_schema = tool_input.get("input_schema")
    code = tool_input.get("code", "")

    if not _NAME_RE.match(name):
        return json.dumps({"error": "name must be lowercase snake_case, 3-64 chars, starting with a letter"})
    if name in ctx.config.tools.builtin:
        return json.dumps({"error": f"{name!r} collides with a builtin tool name"})
    if not description.strip():
        return json.dumps({"error": "description is required"})
    if not isinstance(input_schema, dict):
        return json.dumps({"error": "input_schema must be a JSON schema object"})
    if not code.strip():
        return json.dumps({"error": "code is required"})

    proposal_id = uuid.uuid4().hex[:12]
    proposal = {
        "id": proposal_id,
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "code": code,
        "proposed_at": datetime.now(timezone.utc).isoformat(),
        "session_id": ctx.session_id,
        "status": "pending",
    }
    path = ctx.config.tools.pending_dir / f"{proposal_id}.json"
    path.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    return json.dumps({
        "status": "pending_review",
        "proposal_id": proposal_id,
        "message": (
            f"Proposal filed as {proposal_id}. It will NOT run until a human reviews the "
            f"code and runs `jarvis admin approve {proposal_id}`. Continue the task without "
            "this tool for now, or tell the user it's waiting on approval."
        ),
    })


BUILTIN_SPECS: dict[str, ToolSpec] = {
    "list_dir": ToolSpec(
        name="list_dir",
        description="List files and directories at a path inside your workspace (default: the workspace root).",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace root. Defaults to '.'"}
            },
        },
        handler=_list_dir,
    ),
    "read_file": ToolSpec(
        name="read_file",
        description="Read a UTF-8 text file inside your workspace. Files over 200KB are rejected.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the workspace root."}},
            "required": ["path"],
        },
        handler=_read_file,
    ),
    "write_file": ToolSpec(
        name="write_file",
        description="Write (create or overwrite) a UTF-8 text file inside your workspace. Parent directories are created automatically.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace root."},
                "content": {"type": "string", "description": "Full file contents to write."},
            },
            "required": ["path", "content"],
        },
        handler=_write_file,
    ),
    "run_python": ToolSpec(
        name="run_python",
        description=(
            "Run a Python script from your workspace inside an isolated, resource-limited "
            "sandbox container. No network access unless the session's network mode allows "
            "it. Returns stdout, stderr, and the exit code."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the script, relative to the workspace root."},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command-line arguments to pass to the script.",
                },
            },
            "required": ["path"],
        },
        handler=_run_python,
    ),
    "propose_tool": ToolSpec(
        name="propose_tool",
        description=(
            "Propose a brand-new tool when none of your current tools can do something you "
            "need. This only files a request for a human to review — it never executes code "
            "and the tool is not available until approved. The code must be a standalone "
            "Python script that reads a single JSON object from stdin (matching input_schema) "
            "and prints a single JSON object to stdout as its result; once approved it runs "
            "in the same sandbox as run_python."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "lowercase_snake_case tool name"},
                "description": {"type": "string", "description": "What the tool does and when to use it."},
                "input_schema": {"type": "object", "description": "JSON schema for the tool's input."},
                "code": {
                    "type": "string",
                    "description": "Full Python source implementing the tool (stdin JSON in, stdout JSON out).",
                },
            },
            "required": ["name", "description", "input_schema", "code"],
        },
        handler=_propose_tool,
    ),
}
