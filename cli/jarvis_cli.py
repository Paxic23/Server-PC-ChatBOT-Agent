#!/usr/bin/env python3
"""jarvis-cli: talk to jarvis-server from any machine on the LAN.

Configure via environment variables (put them in your shell profile):
    JARVIS_SERVER_URL   e.g. https://192.168.1.50:8420
    JARVIS_TOKEN        your bearer token (see config/users.yaml on the server)

Usage:
    jarvis chat "what's in my workspace?"
    jarvis send ./my_sim --message "run main.py and summarize the results"
    jarvis new
    jarvis pull results.csv
    jarvis admin review-tools
    jarvis admin approve <proposal_id>
    jarvis admin reject <proposal_id> --reason "..."
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
import websockets

STATE_DIR = Path.home() / ".config" / "jarvis"
STATE_FILE = STATE_DIR / "state.json"


def _server_url() -> str:
    url = os.environ.get("JARVIS_SERVER_URL")
    if not url:
        sys.exit("JARVIS_SERVER_URL is not set (e.g. https://192.168.1.50:8420)")
    return url.rstrip("/")


def _token() -> str:
    token = os.environ.get("JARVIS_TOKEN")
    if not token:
        sys.exit("JARVIS_TOKEN is not set")
    return token


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _current_session(server_url: str) -> Optional[str]:
    return _load_state().get(server_url)


def _set_current_session(server_url: str, session_id: str) -> None:
    state = _load_state()
    state[server_url] = session_id
    _save_state(state)


def _create_session(server_url: str, network_mode: Optional[str]) -> str:
    resp = requests.post(
        f"{server_url}/v1/sessions", json={"network_mode": network_mode}, headers=_headers(), timeout=30
    )
    resp.raise_for_status()
    session_id = resp.json()["session_id"]
    _set_current_session(server_url, session_id)
    print(f"[jarvis] new session {session_id}", file=sys.stderr)
    return session_id


def _get_or_create_session(server_url: str, force_new: bool, network_mode: Optional[str]) -> str:
    if not force_new:
        existing = _current_session(server_url)
        if existing:
            return existing
    return _create_session(server_url, network_mode)


def _ws_url(server_url: str, session_id: str) -> str:
    parsed = urlparse(server_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/v1/sessions/{session_id}/stream"


async def _send_message(server_url: str, session_id: str, message: str) -> None:
    async with websockets.connect(_ws_url(server_url, session_id)) as ws:
        await ws.send(json.dumps({"token": _token()}))
        ready = json.loads(await ws.recv())
        if ready.get("type") == "error":
            sys.exit(f"[jarvis] {ready['data'].get('message')}")

        await ws.send(json.dumps({"message": message}))
        while True:
            event = json.loads(await ws.recv())
            etype, data = event["type"], event["data"]
            if etype == "tool_call":
                print(f"  -> {data['name']}({json.dumps(data['input'])})", file=sys.stderr)
            elif etype == "final":
                print(data["text"])
                return
            elif etype == "error":
                sys.exit(f"[jarvis] error: {data.get('message')}")


def _zip_path(source: Path) -> Path:
    fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    tmp = Path(tmp_name)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        if source.is_dir():
            for f in source.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(source))
        else:
            zf.write(source, source.name)
    return tmp


def cmd_send(args: argparse.Namespace) -> None:
    server_url = _server_url()
    session_id = _get_or_create_session(server_url, args.new, args.network)
    source = Path(args.path).expanduser().resolve()
    if not source.exists():
        sys.exit(f"{source} does not exist")

    zip_path = _zip_path(source)
    try:
        with open(zip_path, "rb") as f:
            resp = requests.post(
                f"{server_url}/v1/sessions/{session_id}/files",
                headers=_headers(),
                files={"file": ("upload.zip", f, "application/zip")},
                timeout=120,
            )
        resp.raise_for_status()
        extracted = resp.json()["extracted"]
        print(f"[jarvis] uploaded {len(extracted)} file(s)", file=sys.stderr)
    finally:
        zip_path.unlink(missing_ok=True)

    asyncio.run(_send_message(server_url, session_id, args.message))


def cmd_chat(args: argparse.Namespace) -> None:
    server_url = _server_url()
    session_id = _get_or_create_session(server_url, args.new, args.network)
    asyncio.run(_send_message(server_url, session_id, args.message))


def cmd_new(args: argparse.Namespace) -> None:
    _create_session(_server_url(), args.network)


def cmd_pull(args: argparse.Namespace) -> None:
    server_url = _server_url()
    session_id = _current_session(server_url)
    if not session_id:
        sys.exit("no active session — run `jarvis chat` or `jarvis send` first")
    resp = requests.get(
        f"{server_url}/v1/sessions/{session_id}/files/{args.remote_path}", headers=_headers(), timeout=60
    )
    resp.raise_for_status()
    out = Path(args.out or Path(args.remote_path).name)
    out.write_bytes(resp.content)
    print(f"[jarvis] wrote {out}")


def cmd_admin_review(args: argparse.Namespace) -> None:
    server_url = _server_url()
    resp = requests.get(f"{server_url}/v1/admin/tools/pending", headers=_headers(), timeout=30)
    resp.raise_for_status()
    pending = resp.json()
    if not pending:
        print("no pending tool proposals")
        return
    for p in pending:
        print(f"{p['id']}  {p['name']:<24} {p['description']}")
        if args.verbose:
            detail = requests.get(
                f"{server_url}/v1/admin/tools/pending/{p['id']}", headers=_headers(), timeout=30
            )
            detail.raise_for_status()
            print("--- code ---")
            print(detail.json()["code"])
            print("------------")


def cmd_admin_approve(args: argparse.Namespace) -> None:
    resp = requests.post(f"{_server_url()}/v1/admin/tools/{args.proposal_id}/approve", headers=_headers(), timeout=30)
    resp.raise_for_status()
    print(resp.json())


def cmd_admin_reject(args: argparse.Namespace) -> None:
    resp = requests.post(
        f"{_server_url()}/v1/admin/tools/{args.proposal_id}/reject",
        headers=_headers(),
        json={"reason": args.reason},
        timeout=30,
    )
    resp.raise_for_status()
    print(resp.json())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis")
    sub = parser.add_subparsers(dest="command", required=True)

    p_chat = sub.add_parser("chat", help="send a text message, no files")
    p_chat.add_argument("message")
    p_chat.add_argument("--new", action="store_true", help="start a new session first")
    p_chat.add_argument("--network", choices=["none", "restricted", "full"], help="override this session's sandbox network mode")
    p_chat.set_defaults(func=cmd_chat)

    p_send = sub.add_parser("send", help="upload a file or folder, then send a message")
    p_send.add_argument("path")
    p_send.add_argument("--message", required=True)
    p_send.add_argument("--new", action="store_true")
    p_send.add_argument("--network", choices=["none", "restricted", "full"])
    p_send.set_defaults(func=cmd_send)

    p_new = sub.add_parser("new", help="start a fresh session")
    p_new.add_argument("--network", choices=["none", "restricted", "full"])
    p_new.set_defaults(func=cmd_new)

    p_pull = sub.add_parser("pull", help="download a file from the current session's workspace")
    p_pull.add_argument("remote_path")
    p_pull.add_argument("--out", help="local destination path (default: same filename)")
    p_pull.set_defaults(func=cmd_pull)

    p_admin = sub.add_parser("admin", help="admin-only commands (requires an admin token)")
    admin_sub = p_admin.add_subparsers(dest="admin_command", required=True)

    p_review = admin_sub.add_parser("review-tools", help="list tools proposed by the agent, pending approval")
    p_review.add_argument("-v", "--verbose", action="store_true", help="also print each proposal's code")
    p_review.set_defaults(func=cmd_admin_review)

    p_approve = admin_sub.add_parser("approve", help="approve a pending tool proposal")
    p_approve.add_argument("proposal_id")
    p_approve.set_defaults(func=cmd_admin_approve)

    p_reject = admin_sub.add_parser("reject", help="reject a pending tool proposal")
    p_reject.add_argument("proposal_id")
    p_reject.add_argument("--reason")
    p_reject.set_defaults(func=cmd_admin_reject)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            pass
        sys.exit(f"[jarvis] HTTP {exc.response.status_code}: {detail or exc}")


if __name__ == "__main__":
    main()
