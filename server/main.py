"""jarvis-server entrypoint.

Run with:
    python -m server.main
or:
    uvicorn server.main:app --host 0.0.0.0 --port 8420
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from server import admin, workspace
from server.agent import AgentCore
from server.audit import log as audit_log
from server.auth import AuthStore, User, get_current_user, is_in_allowed_subnet, require_admin
from server.config import AppConfig, load_config
from server.models import (
    CreateSessionRequest,
    CreateSessionResponse,
    PendingToolDetail,
    PendingToolSummary,
    RejectToolRequest,
    UploadResponse,
)
from server.sessions import SESSION_FILE_NAME, SessionStore
from server.tools.builtin import BUILTIN_SPECS
from server.tools.registry import ToolRegistry

app = FastAPI(title="jarvis-server")


@app.on_event("startup")
def startup() -> None:
    config = load_config()
    app.state.config = config
    app.state.auth_store = AuthStore(config)
    app.state.sessions = SessionStore(config)
    app.state.registry = ToolRegistry(config, BUILTIN_SPECS)
    app.state.agent = AgentCore(config, app.state.registry)


@app.post("/v1/sessions", response_model=CreateSessionResponse)
def create_session(body: CreateSessionRequest, user: User = Depends(get_current_user)):
    session = app.state.sessions.create(user.name, network_mode=body.network_mode)
    return CreateSessionResponse(session_id=session.id, network_mode=session.network_mode, created_at=session.created_at)


@app.post("/v1/sessions/{session_id}/files", response_model=UploadResponse)
async def upload_files(session_id: str, file: UploadFile, user: User = Depends(get_current_user)):
    config: AppConfig = app.state.config
    session = app.state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    max_bytes = config.workspace.max_upload_mb * 1024 * 1024
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp_path = Path(tmp.name)
        total = 0
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"upload exceeds {config.workspace.max_upload_mb}MB limit")
            tmp.write(chunk)

    try:
        extracted = workspace.extract_zip_safely(tmp_path, session.workspace_root)
    except workspace.PathEscapeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    audit_log(config.logging.audit_log, "upload", session_id=session_id, user=user.name, files=extracted)
    return UploadResponse(extracted=extracted)


@app.get("/v1/sessions/{session_id}/files/{path:path}")
def download_file(session_id: str, path: str, user: User = Depends(get_current_user)):
    session = app.state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        target = workspace.resolve_safe_path(session.workspace_root, path, must_exist=True)
    except (workspace.PathEscapeError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"{path!r} is not a file")
    return FileResponse(target)


@app.get("/v1/sessions/{session_id}/archive")
def download_archive(session_id: str, path: str = ".", user: User = Depends(get_current_user)):
    """Zip the session workspace (or a subfolder of it) and stream it back —
    the counterpart to file upload for getting an entire modified project
    back out in one shot instead of pulling files one at a time."""
    config: AppConfig = app.state.config
    session = app.state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        target = workspace.resolve_safe_path(session.workspace_root, path, must_exist=True)
    except (workspace.PathEscapeError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"{path!r} is not a directory")

    fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp_name)
    file_count = workspace.zip_directory(target, tmp_path, exclude={SESSION_FILE_NAME})

    audit_log(config.logging.audit_log, "download_archive", session_id=session_id, user=user.name, path=path, file_count=file_count)
    return FileResponse(
        tmp_path,
        filename=f"jarvis-{session_id}.zip",
        media_type="application/zip",
        background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)),
    )


@app.websocket("/v1/sessions/{session_id}/stream")
async def stream(websocket: WebSocket, session_id: str):
    """Protocol: client connects, sends {"token": "..."} first, then any
    number of {"message": "..."} turns. Server replies with a stream of
    {"type": "tool_call"|"tool_result"|"final"|"error"|"ready", "data": {...}}.
    Auth arrives as the first message (not a query param) so it never ends
    up in server access logs or shell history."""
    await websocket.accept()
    config: AppConfig = app.state.config

    try:
        auth_msg = await websocket.receive_json()
    except Exception:
        await websocket.close(code=4001)
        return

    client_ip = websocket.client.host if websocket.client else None
    user = None
    if is_in_allowed_subnet(client_ip, config):
        user = app.state.auth_store.resolve(auth_msg.get("token", ""))
    if user is None:
        await websocket.send_json({"type": "error", "data": {"message": "unauthorized"}})
        await websocket.close(code=4001)
        return

    session = app.state.sessions.get(session_id)
    if session is None:
        await websocket.send_json({"type": "error", "data": {"message": "session not found"}})
        await websocket.close(code=4004)
        return

    await websocket.send_json({"type": "ready", "data": {}})

    try:
        while True:
            msg = await websocket.receive_json()
            user_message = msg.get("message", "")
            if not user_message:
                continue
            async for event in app.state.agent.run_turn(session=session, user_message=user_message, username=user.name):
                await websocket.send_json({"type": event.type, "data": event.data})
            app.state.sessions.save(session)
    except WebSocketDisconnect:
        pass


# --- Admin: tool approval ---------------------------------------------------

@app.get("/v1/admin/tools/pending", response_model=list[PendingToolSummary])
def admin_list_pending(user: User = Depends(require_admin)):
    proposals = admin.list_pending(app.state.config)
    return [
        PendingToolSummary(**{k: p[k] for k in ("id", "name", "description", "proposed_at", "session_id")})
        for p in proposals
    ]


@app.get("/v1/admin/tools/pending/{proposal_id}", response_model=PendingToolDetail)
def admin_get_pending(proposal_id: str, user: User = Depends(require_admin)):
    try:
        proposal = admin.get_pending(app.state.config, proposal_id)
    except admin.ToolApprovalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PendingToolDetail(**proposal)


@app.post("/v1/admin/tools/{proposal_id}/approve")
def admin_approve(proposal_id: str, user: User = Depends(require_admin)):
    try:
        return admin.approve(app.state.config, app.state.registry, proposal_id, approved_by=user.name)
    except admin.ToolApprovalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/admin/tools/{proposal_id}/reject")
def admin_reject(proposal_id: str, body: RejectToolRequest, user: User = Depends(require_admin)):
    try:
        return admin.reject(app.state.config, proposal_id, rejected_by=user.name, reason=body.reason)
    except admin.ToolApprovalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def main() -> None:
    config = load_config()
    ssl_kwargs = {}
    if config.server.tls.enabled:
        ssl_kwargs = {
            "ssl_certfile": str(config.server.tls.cert_file),
            "ssl_keyfile": str(config.server.tls.key_file),
        }
    uvicorn.run("server.main:app", host=config.server.host, port=config.server.port, **ssl_kwargs)


if __name__ == "__main__":
    main()
