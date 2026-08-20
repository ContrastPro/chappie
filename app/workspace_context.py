"""Bounded user-supplied workspace context for one Project QA request."""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.schemas import FactV1, digest

WORKSPACE_CONTEXT_IDENTITY = "workspace_context/1.0.0"
MAX_WORKSPACE_TEXT_CHARACTERS = 12_000


@dataclass(frozen=True, slots=True)
class WorkspaceRangeV1:
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        if not 0 <= self.start_line <= self.end_line:
            raise ValueError("workspace range is invalid")

    def payload(self) -> dict[str, int]:
        return {"start_line": self.start_line, "end_line": self.end_line}


@dataclass(frozen=True, slots=True)
class WorkspaceContextV1:
    workspace_name: str
    workspace_id: str
    active_file_relative_path: str | None = None
    language_id: str | None = None
    selected_text: str | None = None
    selected_text_range: WorkspaceRangeV1 | None = None
    active_file_excerpt: str | None = None
    excerpt_range: WorkspaceRangeV1 | None = None
    git_branch: str | None = None
    git_commit: str | None = None
    git_dirty: bool | None = None
    selection_truncated: bool = False
    excerpt_truncated: bool = False

    def __post_init__(self) -> None:
        if not self.workspace_name or not self.workspace_id:
            raise ValueError("workspace identity is required")
        if self.active_file_relative_path is not None:
            _relative_path(self.active_file_relative_path)
        if self.selected_text is not None and self.active_file_excerpt is not None:
            raise ValueError("selection and excerpt are mutually exclusive")
        if self.selected_text is not None:
            _bounded_text(self.selected_text)
            if self.selected_text_range is None:
                raise ValueError("selection range is required")
        if self.active_file_excerpt is not None:
            _bounded_text(self.active_file_excerpt)
            if self.excerpt_range is None:
                raise ValueError("excerpt range is required")
        if self.git_commit is not None and (
            len(self.git_commit) < 7
            or any(char not in "0123456789abcdef" for char in self.git_commit.lower())
        ):
            raise ValueError("git commit must be a hash")
        if self.git_dirty is not None and not isinstance(self.git_dirty, bool):
            raise ValueError("git dirty must be boolean")

    @property
    def context_digest(self) -> str:
        return digest({"identity": WORKSPACE_CONTEXT_IDENTITY, **self._payload()})

    def payload(self) -> dict[str, object]:
        return {**self._payload(), "context_digest": self.context_digest}

    def _payload(self) -> dict[str, object]:
        return {
            "schema": WORKSPACE_CONTEXT_IDENTITY,
            "workspace_name": self.workspace_name,
            "workspace_id": self.workspace_id,
            "active_file_relative_path": self.active_file_relative_path,
            "language_id": self.language_id,
            "selected_text": self.selected_text,
            "selected_text_range": self.selected_text_range.payload()
            if self.selected_text_range
            else None,
            "active_file_excerpt": self.active_file_excerpt,
            "excerpt_range": self.excerpt_range.payload() if self.excerpt_range else None,
            "git_branch": self.git_branch,
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "selection_truncated": self.selection_truncated,
            "excerpt_truncated": self.excerpt_truncated,
        }

    def facts(self, timestamp) -> tuple[FactV1, ...]:
        values = (
            ("workspace.id", self.workspace_id),
            ("workspace.file.path", self.active_file_relative_path),
            ("workspace.language", self.language_id),
            ("workspace.selection.text", self.selected_text),
            ("workspace.excerpt.text", self.active_file_excerpt),
            ("workspace.git.branch", self.git_branch),
            ("workspace.git.commit", self.git_commit),
            (
                "workspace.git.dirty",
                str(self.git_dirty).lower() if self.git_dirty is not None else None,
            ),
        )
        return tuple(
            FactV1(
                key,
                "USER_SUPPLIED_WORKSPACE_CONTEXT",
                timestamp,
                key,
                value,
                None,
                self.context_digest,
                canonical_field=key,
                value_type="STRING",
            )
            for key, value in values
            if value is not None
        )


def parse_workspace_context(value: object) -> WorkspaceContextV1:
    if not isinstance(value, dict):
        raise TypeError("workspace context must be an object")
    allowed = {
        "schema",
        "workspace_name",
        "workspace_id",
        "active_file_relative_path",
        "language_id",
        "selected_text",
        "selected_text_range",
        "active_file_excerpt",
        "excerpt_range",
        "git_branch",
        "git_commit",
        "git_dirty",
        "selection_truncated",
        "excerpt_truncated",
        "context_digest",
    }
    if set(value).difference(allowed) or value.get("schema") != WORKSPACE_CONTEXT_IDENTITY:
        raise ValueError("workspace context schema is invalid")
    result = WorkspaceContextV1(
        workspace_name=_string(value, "workspace_name", required=True),
        workspace_id=_string(value, "workspace_id", required=True),
        active_file_relative_path=_string(value, "active_file_relative_path"),
        language_id=_string(value, "language_id"),
        selected_text=_string(value, "selected_text"),
        selected_text_range=_range(value.get("selected_text_range")),
        active_file_excerpt=_string(value, "active_file_excerpt"),
        excerpt_range=_range(value.get("excerpt_range")),
        git_branch=_string(value, "git_branch"),
        git_commit=_string(value, "git_commit"),
        git_dirty=value.get("git_dirty"),
        selection_truncated=bool(value.get("selection_truncated", False)),
        excerpt_truncated=bool(value.get("excerpt_truncated", False)),
    )
    if value.get("context_digest") != result.context_digest:
        raise ValueError("workspace context digest is invalid")
    return result


def _range(value: object) -> WorkspaceRangeV1 | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"start_line", "end_line"}:
        raise ValueError("workspace range is invalid")
    return WorkspaceRangeV1(value["start_line"], value["end_line"])


def _string(value: dict[str, object], key: str, *, required: bool = False) -> str | None:
    result = value.get(key)
    if result is None and not required:
        return None
    if not isinstance(result, str) or (required and not result):
        raise ValueError(f"workspace {key} is invalid")
    return result


def _relative_path(value: str) -> None:
    if value.startswith(("/", "\\")) or ":" in value or ".." in value.replace("\\", "/").split("/"):
        raise ValueError("workspace path must be normalized and relative")


def _bounded_text(value: str) -> None:
    if not value or len(value) > MAX_WORKSPACE_TEXT_CHARACTERS:
        raise ValueError("workspace text exceeds limit")
