"""Auth: bearer token -> user, plus a LAN-subnet allowlist enforced on every
request regardless of whether the token is valid. Tokens live in env vars
(referenced by name from config/users.yaml), never in the yaml itself.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Optional

import yaml
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from server.config import AppConfig

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class User:
    name: str
    role: str  # "admin" | "user"


class AuthStore:
    def __init__(self, config: AppConfig):
        self.config = config
        self._token_to_user: dict[str, User] = {}
        self.reload()

    def reload(self) -> None:
        users_file = self.config.auth.users_file
        if not users_file.exists():
            raise RuntimeError(
                f"{users_file} not found — copy config/users.example.yaml to "
                "config/users.yaml and set at least one admin user."
            )
        entries = yaml.safe_load(users_file.read_text(encoding="utf-8")) or []
        token_to_user: dict[str, User] = {}
        for entry in entries:
            token = os.environ.get(entry["token_env"])
            if not token:
                continue
            token_to_user[token] = User(name=entry["name"], role=entry.get("role", "user"))
        if not token_to_user:
            raise RuntimeError(
                f"no usable users found via {users_file} — check that the token_env "
                "variables referenced there are actually set (.env / systemd unit)."
            )
        self._token_to_user = token_to_user

    def resolve(self, token: str) -> Optional[User]:
        return self._token_to_user.get(token)


def is_in_allowed_subnet(client_host: Optional[str], config: AppConfig) -> bool:
    if client_host is None:
        return False
    try:
        return ipaddress.ip_address(client_host) in config.server.allowed_subnet
    except ValueError:
        return False


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> User:
    config: AppConfig = request.app.state.config
    auth_store: AuthStore = request.app.state.auth_store

    client_ip = request.client.host if request.client else None
    if not is_in_allowed_subnet(client_ip, config):
        raise HTTPException(status_code=403, detail="client is outside the allowed LAN subnet")

    if credentials is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    user = auth_store.resolve(credentials.credentials)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return user
