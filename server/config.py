"""Loads config/config.yaml + .env into a typed AppConfig."""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

VALID_NETWORK_MODES = ("none", "restricted", "full")


def validate_network_mode(mode: str) -> str:
    if mode not in VALID_NETWORK_MODES:
        raise ValueError(f"invalid network mode {mode!r}, must be one of {VALID_NETWORK_MODES}")
    return mode


@dataclass
class TLSConfig:
    enabled: bool
    cert_file: Path
    key_file: Path


@dataclass
class ServerConfig:
    host: str
    port: int
    allowed_subnet: ipaddress.IPv4Network
    tls: TLSConfig


@dataclass
class AgentConfig:
    provider: str
    model: str
    max_tokens: int
    max_tool_iterations: int
    system_prompt_file: Path


@dataclass
class WorkspaceConfig:
    root: Path
    max_upload_mb: int
    session_ttl_hours: int


@dataclass
class SandboxConfig:
    backend: str
    image: str
    network: str
    restricted_proxy_url: str
    cpu_limit: str
    memory_limit: str
    pids_limit: int
    timeout_seconds: int
    run_as_uid: int


@dataclass
class ToolsConfig:
    builtin: list
    pending_dir: Path
    approved_dir: Path
    require_human_approval: bool


@dataclass
class AuthConfig:
    users_file: Path


@dataclass
class LoggingConfig:
    audit_log: Path
    level: str


@dataclass
class AppConfig:
    server: ServerConfig
    agent: AgentConfig
    workspace: WorkspaceConfig
    sandbox: SandboxConfig
    tools: ToolsConfig
    auth: AuthConfig
    logging: LoggingConfig
    anthropic_api_key: str
    repo_root: Path

    def ensure_dirs(self) -> None:
        for p in (
            self.workspace.root,
            self.tools.pending_dir,
            self.tools.approved_dir,
            self.logging.audit_log.parent,
        ):
            p.mkdir(parents=True, exist_ok=True)


def load_config(config_path: str | Path = "config/config.yaml", env_path: str | Path | None = None) -> AppConfig:
    repo_root = Path(__file__).resolve().parent.parent
    config_path = Path(config_path)
    if not config_path.is_absolute():
        config_path = repo_root / config_path

    load_dotenv(env_path or (repo_root / ".env"))

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    def resolve(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (repo_root / path)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")

    server_raw = raw["server"]
    tls_raw = server_raw["tls"]

    cfg = AppConfig(
        server=ServerConfig(
            host=server_raw["host"],
            port=int(server_raw["port"]),
            allowed_subnet=ipaddress.ip_network(server_raw["allowed_subnet"], strict=False),
            tls=TLSConfig(
                enabled=bool(tls_raw.get("enabled", False)),
                cert_file=resolve(tls_raw["cert_file"]),
                key_file=resolve(tls_raw["key_file"]),
            ),
        ),
        agent=AgentConfig(
            provider=raw["agent"]["provider"],
            model=raw["agent"]["model"],
            max_tokens=int(raw["agent"]["max_tokens"]),
            max_tool_iterations=int(raw["agent"].get("max_tool_iterations", 25)),
            system_prompt_file=resolve(raw["agent"]["system_prompt_file"]),
        ),
        workspace=WorkspaceConfig(
            root=resolve(raw["workspace"]["root"]),
            max_upload_mb=int(raw["workspace"]["max_upload_mb"]),
            session_ttl_hours=int(raw["workspace"]["session_ttl_hours"]),
        ),
        sandbox=SandboxConfig(
            backend=raw["sandbox"]["backend"],
            image=raw["sandbox"]["image"],
            network=validate_network_mode(raw["sandbox"]["network"]),
            restricted_proxy_url=raw["sandbox"]["restricted_proxy_url"],
            cpu_limit=str(raw["sandbox"]["cpu_limit"]),
            memory_limit=str(raw["sandbox"]["memory_limit"]),
            pids_limit=int(raw["sandbox"]["pids_limit"]),
            timeout_seconds=int(raw["sandbox"]["timeout_seconds"]),
            run_as_uid=int(raw["sandbox"]["run_as_uid"]),
        ),
        tools=ToolsConfig(
            builtin=list(raw["tools"]["builtin"]),
            pending_dir=resolve(raw["tools"]["pending_dir"]),
            approved_dir=resolve(raw["tools"]["approved_dir"]),
            require_human_approval=bool(raw["tools"].get("require_human_approval", True)),
        ),
        auth=AuthConfig(users_file=resolve(raw["auth"]["users_file"])),
        logging=LoggingConfig(
            audit_log=resolve(raw["logging"]["audit_log"]),
            level=raw["logging"].get("level", "INFO"),
        ),
        anthropic_api_key=api_key,
        repo_root=repo_root,
    )

    if not cfg.tools.require_human_approval:
        # Hardcoded refusal to boot misconfigured: tool approval is a safety
        # control, not a knob the agent (or an accidental config edit) can
        # quietly disable.
        raise RuntimeError(
            "tools.require_human_approval must stay true — new-tool approval is "
            "not an agent-configurable safety control."
        )

    cfg.ensure_dirs()
    return cfg
