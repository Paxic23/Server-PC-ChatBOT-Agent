"""Tool registry.

Builtin tools are trusted Python callables running in-process. Approved
custom tools are never imported or exec'd in-process — they run through
sandbox.run_tool_script exactly like run_python does, so human approval
grants "this logic may run inside the sandbox", never "this code may touch
the host."
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from server import sandbox
from server.config import AppConfig


@dataclass
class ToolContext:
    session_id: str
    workspace_root: Path
    config: AppConfig
    network_mode: str


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Optional[Callable[[dict, ToolContext], str]] = None  # builtin
    script_path: Optional[Path] = None                             # approved custom


class ToolRegistry:
    def __init__(self, config: AppConfig, builtin_specs: dict[str, ToolSpec]):
        self.config = config
        self._tools: dict[str, ToolSpec] = dict(builtin_specs)
        self.reload_approved()

    def reload_approved(self) -> None:
        """Pick up any tools approved since the registry was built (or last
        reload). Called at startup and again right after every approval."""
        approved_dir = self.config.tools.approved_dir
        for meta_file in sorted(approved_dir.glob("*.json")):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            name = meta.get("name")
            script_path = approved_dir / f"{name}.py"
            if not name or not script_path.exists():
                continue
            existing = self._tools.get(name)
            if existing is not None and existing.handler is not None:
                continue  # never let an approved tool shadow a builtin
            self._tools[name] = ToolSpec(
                name=name,
                description=meta["description"],
                input_schema=meta["input_schema"],
                script_path=script_path,
            )

    def schemas(self) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in self._tools.values()
        ]

    def has(self, name: str) -> bool:
        return name in self._tools

    def call(self, name: str, tool_input: dict, ctx: ToolContext) -> str:
        spec = self._tools.get(name)
        if spec is None:
            return json.dumps({"error": f"unknown tool {name!r}"})

        if spec.handler is not None:
            return spec.handler(tool_input, ctx)

        result = sandbox.run_tool_script(
            ctx.config.sandbox,
            ctx.workspace_root,
            spec.script_path,
            json.dumps(tool_input),
            network_mode=ctx.network_mode,
        )
        if result.timed_out:
            return json.dumps({"error": "tool timed out", "stderr": result.stderr[-2000:]})
        if result.exit_code != 0:
            return json.dumps({"error": f"tool exited {result.exit_code}", "stderr": result.stderr[-2000:]})
        return result.stdout.strip() or json.dumps({"error": "tool produced no output"})
