"""Session management.

One session = one conversation + one jailed workspace directory
(runtime/workspace/<session_id>/). History and metadata persist to a JSON
file inside that same directory so a server restart doesn't lose context.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from server import workspace
from server.config import AppConfig, validate_network_mode

SESSION_FILE_NAME = ".jarvis_session.json"


@dataclass
class Session:
    id: str
    username: str
    workspace_root: Path
    network_mode: str
    created_at: str
    history: list = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["workspace_root"] = str(self.workspace_root)
        return d


class SessionStore:
    def __init__(self, config: AppConfig):
        self.config = config
        self._sessions: dict[str, Session] = {}

    def create(self, username: str, network_mode: Optional[str] = None) -> Session:
        session_id = uuid.uuid4().hex[:16]
        root = workspace.session_root(self.config.workspace.root, session_id)
        mode = validate_network_mode(network_mode) if network_mode else self.config.sandbox.network
        session = Session(
            id=session_id,
            username=username,
            workspace_root=root,
            network_mode=mode,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._sessions[session_id] = session
        self.save(session)
        return session

    def get(self, session_id: str) -> Optional[Session]:
        if session_id in self._sessions:
            return self._sessions[session_id]

        root = self.config.workspace.root / session_id
        session_file = root / SESSION_FILE_NAME
        if not session_file.exists():
            return None

        data = json.loads(session_file.read_text(encoding="utf-8"))
        session = Session(
            id=data["id"],
            username=data["username"],
            workspace_root=Path(data["workspace_root"]),
            network_mode=data["network_mode"],
            created_at=data["created_at"],
            history=data.get("history", []),
        )
        self._sessions[session_id] = session
        return session

    def save(self, session: Session) -> None:
        session_file = session.workspace_root / SESSION_FILE_NAME
        session_file.write_text(json.dumps(session.to_json(), indent=2), encoding="utf-8")
