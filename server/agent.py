"""Agent core: the Claude Haiku 4.5 tool-use loop.

run_turn() is an async generator so the server can stream progress (each
tool call as it happens) to the client instead of going silent until the
whole turn is done. It mutates session.history in place; the caller is
responsible for persisting the session afterwards (see server/sessions.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

import anthropic

from server.audit import log as audit_log
from server.config import AppConfig
from server.sessions import Session
from server.tools.registry import ToolContext, ToolRegistry


@dataclass
class AgentEvent:
    type: str  # "tool_call" | "tool_result" | "final" | "error"
    data: dict


class AgentCore:
    def __init__(self, config: AppConfig, registry: ToolRegistry):
        self.config = config
        self.registry = registry
        self.client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
        self._system_prompt = config.agent.system_prompt_file.read_text(encoding="utf-8")

    async def run_turn(self, *, session: Session, user_message: str, username: str) -> AsyncIterator[AgentEvent]:
        ctx = ToolContext(
            session_id=session.id,
            workspace_root=session.workspace_root,
            config=self.config,
            network_mode=session.network_mode,
        )
        messages = session.history + [{"role": "user", "content": user_message}]
        tools = self.registry.schemas()

        audit_log(self.config.logging.audit_log, "message_in", session_id=session.id, user=username, text=user_message)

        for _ in range(self.config.agent.max_tool_iterations):
            try:
                response = await self.client.messages.create(
                    model=self.config.agent.model,
                    max_tokens=self.config.agent.max_tokens,
                    system=self._system_prompt,
                    messages=messages,
                    tools=tools,
                )
            except anthropic.APIError as exc:
                yield AgentEvent("error", {"message": f"Anthropic API error: {exc}"})
                session.history = messages
                return

            # Converted to plain dicts immediately: this keeps session.history
            # JSON-serializable as-is (needed for persistence across restarts)
            # and the Anthropic SDK accepts dict content blocks just as well
            # as the pydantic objects it hands back.
            assistant_content = [block.model_dump() for block in response.content]
            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason != "tool_use":
                final_text = "".join(b["text"] for b in assistant_content if b["type"] == "text")
                audit_log(self.config.logging.audit_log, "message_out", session_id=session.id, text=final_text)
                yield AgentEvent("final", {"text": final_text})
                session.history = messages
                return

            tool_results = []
            for block in assistant_content:
                if block["type"] != "tool_use":
                    continue
                name, tool_input, block_id = block["name"], block["input"], block["id"]
                yield AgentEvent("tool_call", {"name": name, "input": tool_input, "id": block_id})
                try:
                    result_text = self.registry.call(name, tool_input, ctx)
                except Exception as exc:  # tool failures must not crash the turn
                    result_text = f'{{"error": "tool raised an exception: {exc}"}}'
                audit_log(
                    self.config.logging.audit_log,
                    "tool_call",
                    session_id=session.id,
                    tool=name,
                    input=tool_input,
                    result=result_text[:4000],
                )
                yield AgentEvent("tool_result", {"name": name, "id": block_id, "result": result_text})
                tool_results.append({"type": "tool_result", "tool_use_id": block_id, "content": result_text})

            messages.append({"role": "user", "content": tool_results})

        yield AgentEvent(
            "error",
            {"message": f"stopped after {self.config.agent.max_tool_iterations} tool iterations without a final answer"},
        )
        session.history = messages
