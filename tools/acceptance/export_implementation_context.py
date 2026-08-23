"""Export the real neutral DiffReview implementation context as local evidence."""

from __future__ import annotations

import json
from pathlib import Path

from app.implementation_context import (
    ImplementationContextSufficiencyStateV1,
    assess_implementation_context_sufficiency,
)
from app.knowledge.store import KNOWLEDGE_STORE_PATH, committed_snapshot, read_store
from tests.test_real_diff_review_context import materialize_real_diff_review_context

EXPORT_IDENTITY = "chappie_implementation_context_export/1.0.0"
OUTPUT = Path("data/acceptance/implementation_context/diff_review")


def export_real_diff_review_context(root: Path = Path(".")) -> dict[str, object]:
    snapshot = committed_snapshot(root)
    _, manifest = read_store(root / KNOWLEDGE_STORE_PATH)
    context = materialize_real_diff_review_context(root)
    sufficiency = assess_implementation_context_sufficiency(context)
    if (
        not snapshot.clean
        or manifest.snapshot != snapshot
        or context.source_commit != snapshot.commit
    ):
        raise ValueError("CLEAN_SNAPSHOT_REQUIRED")
    if (
        sufficiency.state
        is not ImplementationContextSufficiencyStateV1.IMPLEMENTATION_CONTEXT_SUFFICIENT
    ):
        raise ValueError("IMPLEMENTATION_CONTEXT_NOT_SUFFICIENT")
    if sufficiency.missing_context:
        raise ValueError("MISSING_CONTEXT_REMAINS")
    return {
        "identity": EXPORT_IDENTITY,
        "source_commit": snapshot.commit,
        "implementation_context_identity": context.stable_payload()["identity"],
        "context_digest": context.digest,
        "sufficiency": sufficiency.state.value,
        "canonical_owner": context.canonical_owner,
        "knowledge": {
            "identity": context.knowledge_identity,
            "manifest_digest": context.knowledge_manifest_digest,
            "context_digest": context.knowledge_context_digest,
        },
        "context": context.stable_payload(),
    }


def write_export(root: Path = Path("."), output: Path = OUTPUT) -> tuple[Path, Path]:
    value = export_real_diff_review_context(root)
    target = root / output
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "implementation_context.json"
    json_path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    context = value["context"]
    markdown = "\n".join(
        (
            "# Implementation Context Export",
            f"- Source commit: `{value['source_commit']}`",
            f"- Context digest: `{value['context_digest']}`",
            f"- Canonical owner: `{value['canonical_owner']}`",
            "",
            "## CURRENT",
            *[f"- `{path}`" for path in context["direct_files"]],
            "",
            "## PROPOSED",
            *[
                f"- `{item['identity']}`: {', '.join(item['proposed_delta'])}"
                for item in context["contracts"]
                if item["state"] == "PROPOSED"
            ],
            "",
            "## Authority",
            *[f"- {item}" for item in context["authority"]["forbidden_responsibilities"]],
            "",
            "## Acceptance criteria",
            *[f"- {item}" for item in context["acceptance_criteria"]],
            "",
        )
    )
    md_path = target / "implementation_context.md"
    md_path.write_text(markdown, encoding="utf-8")
    return json_path, md_path


if __name__ == "__main__":
    print(write_export())
