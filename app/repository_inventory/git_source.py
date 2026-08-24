"""Fixed-argv, read-only Git metadata adapter."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.repository_inventory.models import (
    GIT_TIMEOUT_SECONDS,
    MAX_SEMANTIC_ARTIFACT_BYTES,
    safe_path,
)


class GitSourceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TrackedRepositorySnapshotV1:
    commit: str
    clean: bool
    paths: tuple[str, ...]


def _run(root: Path, argv: tuple[str, ...], *, binary: bool = False) -> bytes | str:
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            shell=False,
            check=False,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            text=not binary,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitSourceError("Git repository metadata is unavailable") from exc
    if result.returncode:
        raise GitSourceError("Git repository metadata is unavailable")
    output = result.stdout
    size = len(output) if isinstance(output, bytes) else len(output.encode())
    if size > MAX_SEMANTIC_ARTIFACT_BYTES:
        raise GitSourceError("Git repository metadata exceeds bounded output")
    return output


def read_tracked_snapshot(repository_root: Path) -> TrackedRepositorySnapshotV1:
    root = repository_root.resolve(strict=True)
    commit = str(_run(root, ("git", "rev-parse", "HEAD"))).strip()
    dirty = str(_run(root, ("git", "status", "--porcelain=v1", "--untracked-files=no"))).strip()
    raw = bytes(_run(root, ("git", "ls-files", "-z", "--cached"), binary=True))
    try:
        paths = tuple(
            sorted({safe_path(item.decode("utf-8")) for item in raw.split(b"\0") if item})
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise GitSourceError("Git tracked-file metadata is malformed") from exc
    return TrackedRepositorySnapshotV1(commit=commit, clean=not dirty, paths=paths)
