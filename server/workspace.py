"""Filesystem jail.

Every path a tool touches — whether it comes straight from the model or from
an uploaded zip's file listing — must go through resolve_safe_path() before
any I/O happens. This is enforced here in trusted server code, not left to
the system prompt: the model asking for something outside the jail simply
cannot succeed, regardless of what it says.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Optional


class PathEscapeError(Exception):
    """A requested path would land outside the session workspace."""


def session_root(workspace_root: Path, session_id: str) -> Path:
    root = (workspace_root / session_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_safe_path(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    """Resolve `relative` against the (already-resolved) session `root`.

    Rejects absolute paths and any ".." component outright, then walks the
    path component by component, resolving symlinks as it goes so a symlink
    planted inside the workspace (e.g. by an uploaded script) can't be used
    to hop outside `root` even if the final component doesn't exist yet.
    """
    rel = Path(relative) if relative not in (None, "", ".") else Path(".")
    if rel.is_absolute():
        raise PathEscapeError(f"absolute paths are not allowed: {relative!r}")
    if ".." in rel.parts:
        raise PathEscapeError(f"'..' is not allowed in paths: {relative!r}")

    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink() or current.exists():
            resolved = current.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                raise PathEscapeError(f"path {relative!r} escapes the session workspace") from None
            current = resolved

    if must_exist and not current.exists():
        raise FileNotFoundError(str(current))

    return current


def extract_zip_safely(zip_path: Path, dest_root: Path) -> list[str]:
    """Extract a zip into dest_root, validating every entry with
    resolve_safe_path first (defends against zip-slip). Returns the list of
    extracted relative paths."""
    extracted: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = resolve_safe_path(dest_root, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            extracted.append(info.filename)
    return extracted


def zip_directory(source_dir: Path, dest_zip: Path, exclude: Optional[set] = None) -> int:
    """Zip everything under source_dir (recursively) into dest_zip, using
    paths relative to source_dir. Returns the number of files written.
    `exclude` matches against the file's name and its full relative path, so
    callers can skip internal bookkeeping files (e.g. the session's own
    history file) without leaking them into a downloaded archive."""
    exclude = exclude or set()
    count = 0
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(source_dir.rglob("*")):
            if f.is_dir():
                continue
            rel = f.relative_to(source_dir)
            if f.name in exclude or str(rel) in exclude:
                continue
            zf.write(f, rel)
            count += 1
    return count
