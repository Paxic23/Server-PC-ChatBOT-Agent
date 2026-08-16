"""Docker-based sandbox for executing untrusted code.

Every run gets a brand-new, ephemeral, named container: no network by
default, CPU/memory/pid limits, all capabilities dropped, root filesystem
read-only except the mounted workspace and a small tmpfs /tmp, non-root
user. Approved custom tools (server/tools/registry.py) execute through the
exact same run_tool_script() path as run_python — approval grants "this
logic may run", never "this code may bypass the sandbox."
"""
from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from server.config import SandboxConfig, validate_network_mode


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class SandboxError(Exception):
    pass


def _network_args(cfg: SandboxConfig, network_mode: str) -> list[str]:
    network_mode = validate_network_mode(network_mode)
    if network_mode == "none":
        return ["--network", "none"]
    if network_mode == "full":
        return ["--network", "bridge"]
    # restricted: bridge network, but the only usable egress is the
    # allowlisting proxy (see sandbox_image/restricted-proxy) — the image
    # has no unproxied route out, so this only works if that proxy is up.
    # --add-host wires host.docker.internal to the host gateway on Linux
    # (Docker 20.10+), which is what restricted_proxy_url points at.
    return [
        "--network", "bridge",
        "--add-host", "host.docker.internal:host-gateway",
        "--env", f"HTTP_PROXY={cfg.restricted_proxy_url}",
        "--env", f"HTTPS_PROXY={cfg.restricted_proxy_url}",
        "--env", f"http_proxy={cfg.restricted_proxy_url}",
        "--env", f"https_proxy={cfg.restricted_proxy_url}",
    ]


def _run_container(
    cfg: SandboxConfig,
    *,
    workspace: Path,
    command: list[str],
    network_mode: str,
    stdin_data: str | None = None,
    extra_mounts: list[tuple[Path, str]] | None = None,
) -> RunResult:
    name = f"jarvis-run-{uuid.uuid4().hex[:12]}"
    docker_cmd = [
        "docker", "run", "--rm",
        "--name", name,
        "--user", f"{cfg.run_as_uid}:{cfg.run_as_uid}",
        "--env", "HOME=/tmp",
        "--cpus", cfg.cpu_limit,
        "--memory", cfg.memory_limit,
        "--pids-limit", str(cfg.pids_limit),
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=64m",
        "-v", f"{workspace}:/workspace",
        "-w", "/workspace",
        *_network_args(cfg, network_mode),
    ]
    for host_path, container_path in extra_mounts or []:
        docker_cmd += ["-v", f"{host_path}:{container_path}:ro"]
    docker_cmd.append(cfg.image)
    docker_cmd += command

    try:
        proc = subprocess.run(
            docker_cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=cfg.timeout_seconds,
        )
        return RunResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, timed_out=False)
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["docker", "kill", name], capture_output=True, text=True)

        def _decode(v):
            if v is None:
                return ""
            return v.decode(errors="replace") if isinstance(v, bytes) else v

        return RunResult(
            exit_code=-1,
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr) + "\n[jarvis] execution timed out and the container was killed",
            timed_out=True,
        )
    except FileNotFoundError as exc:
        raise SandboxError(
            "docker executable not found on PATH — install Docker on this host "
            "before running sandboxed code"
        ) from exc


def run_python(
    cfg: SandboxConfig,
    workspace: Path,
    script_rel_path: str,
    args: list[str] | None = None,
    network_mode: str | None = None,
) -> RunResult:
    return _run_container(
        cfg,
        workspace=workspace,
        command=["python3", script_rel_path, *(args or [])],
        network_mode=network_mode or cfg.network,
    )


def run_tool_script(
    cfg: SandboxConfig,
    workspace: Path,
    tool_file: Path,
    input_json: str,
    network_mode: str | None = None,
) -> RunResult:
    """Run an approved custom tool. The reviewed script is mounted read-only
    at a fixed in-container path; its input arrives as JSON on stdin and it
    is expected to print a single JSON object to stdout as its result."""
    return _run_container(
        cfg,
        workspace=workspace,
        command=["python3", "/tool/tool.py"],
        network_mode=network_mode or cfg.network,
        stdin_data=input_json,
        extra_mounts=[(tool_file, "/tool/tool.py")],
    )
