"""Append-only JSONL audit log.

Every message, tool call (with args + result), file upload, and
tool-approval decision gets a line here. This is the record you'd check
after the fact to see exactly what an agent with sandboxed code-execution
access on your machine actually did.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()


def log(audit_log_path: Path, event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    line = json.dumps(entry, default=str)
    with _lock:
        with open(audit_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
