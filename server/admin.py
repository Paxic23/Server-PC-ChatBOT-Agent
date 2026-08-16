"""Human-approval workflow for agent-proposed tools.

`propose_tool` (server/tools/builtin.py) only ever writes a pending proposal
to disk. Nothing here executes proposal code — approve() copies the code
into tools/approved/ verbatim so a human has already read exactly what will
run before it ever does; the registry then picks it up on reload.
"""
from __future__ import annotations

import json
from typing import Optional

from server.audit import log as audit_log
from server.config import AppConfig
from server.tools.registry import ToolRegistry


class ToolApprovalError(Exception):
    pass


def list_pending(config: AppConfig) -> list[dict]:
    proposals = []
    for f in sorted(config.tools.pending_dir.glob("*.json")):
        try:
            proposals.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return proposals


def get_pending(config: AppConfig, proposal_id: str) -> dict:
    f = config.tools.pending_dir / f"{proposal_id}.json"
    if not f.exists():
        raise ToolApprovalError(f"no pending proposal {proposal_id!r}")
    return json.loads(f.read_text(encoding="utf-8"))


def approve(config: AppConfig, registry: ToolRegistry, proposal_id: str, approved_by: str) -> dict:
    proposal = get_pending(config, proposal_id)
    name = proposal["name"]

    if registry.has(name):
        raise ToolApprovalError(
            f"a tool named {name!r} already exists — reject this proposal or have the agent pick a different name"
        )

    approved_dir = config.tools.approved_dir
    (approved_dir / f"{name}.py").write_text(proposal["code"], encoding="utf-8")
    (approved_dir / f"{name}.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": proposal["description"],
                "input_schema": proposal["input_schema"],
                "approved_by": approved_by,
                "approved_from_proposal": proposal_id,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    (config.tools.pending_dir / f"{proposal_id}.json").unlink(missing_ok=True)
    registry.reload_approved()

    audit_log(config.logging.audit_log, "tool_approved", proposal_id=proposal_id, name=name, approved_by=approved_by)
    return {"name": name, "status": "approved"}


def reject(config: AppConfig, proposal_id: str, rejected_by: str, reason: Optional[str]) -> dict:
    proposal = get_pending(config, proposal_id)
    (config.tools.pending_dir / f"{proposal_id}.json").unlink(missing_ok=True)
    audit_log(
        config.logging.audit_log,
        "tool_rejected",
        proposal_id=proposal_id,
        name=proposal["name"],
        rejected_by=rejected_by,
        reason=reason,
    )
    return {"name": proposal["name"], "status": "rejected"}
