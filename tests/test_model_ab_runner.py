"""Deterministic contracts for the focused model A/B acceptance runner."""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


def _runner():
    path = Path(__file__).parents[1] / "tools" / "acceptance" / "run_model_ab.py"
    sys.path.insert(0, str(path.parent))
    spec = spec_from_file_location("model_ab_runner", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _post(
    runner,
    *,
    case_id: str = "x",
    model_id: str = "QWEN3_4B",
    run_ordinal: int = 0,
    status: str = "OK",
):
    return runner._post_inference_evidence(
        case_id=case_id,
        requested_model_id=model_id,
        effective_model_id=model_id if status == "OK" else None,
        run_ordinal=run_ordinal,
        response_status=status,
        data={"state": status, "answer": "Grounded."} if status == "OK" else None,
        error=None,
    )


def test_focused_matrix_is_explicit_and_has_required_sizes():
    runner = _runner()
    assert runner.EVIDENCE_SCHEMA == "chappie_model_ab/2.1.0"
    assert runner.OUTPUT_RELATIVE == Path("data/acceptance/model_ab/focused_2_1")
    assert len(runner._cases()) == 19
    assert runner.MODEL_IDS == ("QWEN3_4B", "QWEN3_14B")
    assert runner.STABILITY_IDS == ("knowledge_file", "selection_control_plane")


def test_pair_gates_context_retrieval_and_effective_model():
    runner = _runner()
    left = {
        "case_id": "x",
        "question": "q",
        "context_kind": "file",
        "locator": "a",
        "context_digest": "d",
        "context_character_count": 1,
        "status": "OK",
        "retrieved_record_ids": ["retrieved-a", "retrieved-b"],
        "retrieved_record_digests": ["digest-a", "digest-b"],
        "expected_retrieval_context_digest": "retrieval",
        "retrieval_context_digest": "retrieval",
        "expected_workspace_context_digest": "workspace",
        "workspace_context_digest": "workspace",
        "supporting_record_ids": ["cited"],
        "effective_model_id": "QWEN3_4B",
    }
    right = {**left, "effective_model_id": "QWEN3_14B"}
    assert runner._compare(left, right)["valid"] is True
    comparison = runner._compare(left, {**right, "supporting_record_ids": ["other"]})
    assert comparison["valid"] is True
    assert comparison["supporting_records_equal"] is False
    post_inference = {
        "supporting_knowledge_fact_ids": ["knowledge.other.statement"],
        "answer_bounded": "different answer",
        "response_evidence_digest": "different",
    }
    assert runner._compare(left, {**right, "post_inference": post_inference})["valid"] is True
    assert (
        runner._compare(left, {**right, "manual_rubric": {"scoring_status": "SCORED"}})["valid"]
        is True
    )
    assert (
        runner._compare(left, {**right, "retrieved_record_ids": ["other"]})[
            "retrieved_records_equal"
        ]
        is False
    )
    for key, value in (
        ("case_id", "other-case"),
        ("retrieved_record_ids", ["retrieved-b", "retrieved-a"]),
        ("retrieved_record_ids", ["retrieved-a", "retrieved-a"]),
        ("retrieved_record_digests", ["digest-a", "other-digest"]),
        ("retrieval_context_digest", "other-retrieval"),
        ("retrieval_context_digest", None),
        ("workspace_context_digest", "other-workspace"),
        ("status", "TIMEOUT"),
        ("status", "VALIDATION_ERROR"),
    ):
        assert runner._compare(left, {**right, key: value})["valid"] is False
    assert (
        runner._compare(left, {**right, "effective_model_id": "QWEN3_4B"})["effective_model_gate"]
        is False
    )


def test_timing_records_timeouts_and_report_is_serializable(tmp_path: Path):
    runner = _runner()
    assert (
        runner._timing(
            [{"elapsed_ms": 2, "status": "OK"}, {"elapsed_ms": 5, "status": "OUTER_TIMEOUT"}]
        )["timeouts"]
        == 1
    )
    payload = {
        "source_commit": "a" * 40,
        "preflight": {"status": "OK"},
        "default_model": "qwen3:4b",
        "summary": {},
        "stability": {},
        "timing_summary": {},
        "timeout_behavior": {},
        "state_isolation": {},
    }
    runner._write(tmp_path, payload)
    assert (tmp_path / "result.json").exists() and "# Chappie focused model A/B" in (
        tmp_path / "report.md"
    ).read_text()


def test_runner_forwards_only_explicit_model_selection() -> None:
    runner = _runner()
    bodies: list[dict[str, object]] = []

    class Client:
        def post(self, _path: str, *, json: dict[str, object], timeout: float):
            bodies.append(json)
            assert timeout == runner.CHAPPIE_TIMEOUT_SECONDS
            return SimpleNamespace(json=lambda: {"data": {"state": "OK", "answer": "Grounded."}})

    case = runner._cases()[0]
    runner._call(Client(), case, "QWEN3_14B", None)
    runner._call(Client(), case, None, None)

    assert bodies[0]["requested_model_id"] == "QWEN3_14B"
    assert "requested_model_id" not in bodies[1]


def test_post_inference_evidence_separates_fact_and_record_ids() -> None:
    runner = _runner()

    class Client:
        def post(self, _path: str, *, json: dict[str, object], timeout: float):
            return SimpleNamespace(
                json=lambda: {
                    "data": {
                        "state": "OK",
                        "answer": "Grounded.",
                        "supporting_knowledge_fact_ids": ["knowledge.one.statement"],
                        "supporting_records": [{"record_id": "one"}],
                        "unknowns": ["none"],
                        "conflicts": [],
                    }
                }
            )

    result = runner._call(Client(), runner._cases()[0], "QWEN3_4B", None)
    post = result["post_inference"]
    assert post["supporting_knowledge_fact_ids"] == ["knowledge.one.statement"]
    assert post["supporting_record_ids"] == ["one"]
    assert result["supporting_knowledge_fact_ids"] == ["knowledge.one.statement"]
    assert result["supporting_record_ids"] == ["one"]
    assert (
        post["response_evidence_digest"]
        == runner._post_inference_evidence(
            case_id=runner._cases()[0].case_id,
            requested_model_id="QWEN3_4B",
            effective_model_id=None,
            run_ordinal=0,
            response_status="OK",
            data={
                "state": "OK",
                "answer": "Grounded.",
                "supporting_knowledge_fact_ids": ["knowledge.one.statement"],
                "supporting_records": [{"record_id": "one"}],
                "unknowns": ["none"],
                "conflicts": [],
            },
            error=None,
        )["response_evidence_digest"]
    )


def test_safe_rejected_output_is_bounded_and_rejects_hidden_fields() -> None:
    runner = _runner()
    safe = runner._safe_rejected_structured_output(
        {
            "answer": "x" * 5_000,
            "supporting_knowledge_fact_ids": ["knowledge.one.statement"],
            "conflicts": [],
            "unknowns": [],
            "confidence": 0.5,
        }
    )
    assert safe and len(safe["answer"]) == runner.MAX_BOUNDED_RESPONSE_CHARACTERS
    assert (
        runner._safe_rejected_structured_output(
            {"answer": "x", "thinking": "hidden provider reasoning"}
        )
        is None
    )


def test_manual_rubric_is_digest_bound_and_invalid_responses_cannot_be_scored() -> None:
    runner = _runner()
    result = {
        "status": "OK",
        "post_inference": _post(runner),
        "manual_rubric": runner._unscored_rubric(),
    }
    runner._validate_manual_rubric(result, result["manual_rubric"])
    scored = {
        "scoring_status": "SCORED",
        "reviewer_type": "HUMAN",
        "reviewer_identity": "reviewer-1",
        "scored_response_evidence_digest": result["post_inference"]["response_evidence_digest"],
        "scores": {dimension: 2 for dimension in runner.RUBRIC_DIMENSIONS},
        "notes": "Bounded review.",
    }
    persisted = runner._score_response(result, scored)
    assert persisted["manual_rubric"] == scored
    with pytest.raises(ValueError, match="not eligible"):
        runner._score_response(persisted, scored)
    for key, value in (
        ("scored_response_evidence_digest", "other"),
        ("scores", {"correctness": 3}),
        ("scores", {dimension: 3 for dimension in runner.RUBRIC_DIMENSIONS}),
    ):
        invalid = {**scored, key: value}
        try:
            runner._validate_manual_rubric(result, invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid manual rubric was accepted")
    invalid_response = {**result, "post_inference": _post(runner, status="TRANSPORT_ERROR")}
    try:
        runner._validate_manual_rubric(invalid_response, scored)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid response was accepted for scoring")


@pytest.mark.parametrize("identity", ({"id": "x"}, ["x"], 1, 1.0, True, "x" * 257))
def test_manual_rubric_rejects_invalid_reviewer_identity(identity: object) -> None:
    runner = _runner()
    rubric = {**runner._unscored_rubric(), "reviewer_identity": identity}
    with pytest.raises(ValueError, match="canonical representation|reviewer identity"):
        runner._validate_manual_rubric({}, rubric)


def test_manual_rubric_requires_exact_unscored_representation() -> None:
    runner = _runner()
    for key, value in (
        ("reviewer_identity", "reviewer"),
        ("scores", {"correctness": 2}),
        ("scored_response_evidence_digest", "digest"),
    ):
        with pytest.raises(ValueError, match="canonical representation"):
            runner._validate_manual_rubric({}, {**runner._unscored_rubric(), key: value})


@pytest.mark.parametrize(
    "mode",
    (
        "missing_dimension",
        "extra_dimension",
        "non_string_notes",
        "oversized_notes",
    ),
)
def test_scored_rubric_rejects_malformed_fields(mode: str) -> None:
    runner = _runner()
    post = _post(runner)
    scores = {dimension: 2 for dimension in runner.RUBRIC_DIMENSIONS}
    if mode == "missing_dimension":
        scores.pop("correctness")
    elif mode == "extra_dimension":
        scores["unexpected"] = 2
    rubric = {
        "scoring_status": "SCORED",
        "reviewer_type": "HUMAN",
        "reviewer_identity": None,
        "scored_response_evidence_digest": post["response_evidence_digest"],
        "scores": scores,
        "notes": 1
        if mode == "non_string_notes"
        else "x" * 2_001
        if mode == "oversized_notes"
        else "",
    }
    result = {
        "status": "OK",
        "post_inference": post,
    }
    with pytest.raises(ValueError):
        runner._validate_manual_rubric(result, rubric)


@pytest.mark.parametrize("bad_score", (3, -1, 1.5, True))
def test_scored_rubric_rejects_out_of_range_and_non_integer_scores(bad_score: object) -> None:
    runner = _runner()
    post = _post(runner)
    rubric = {
        "scoring_status": "SCORED",
        "reviewer_type": "HUMAN",
        "reviewer_identity": None,
        "scored_response_evidence_digest": post["response_evidence_digest"],
        "scores": {dimension: bad_score for dimension in runner.RUBRIC_DIMENSIONS},
        "notes": "",
    }
    with pytest.raises(ValueError, match="scores"):
        runner._validate_manual_rubric(
            {
                "status": "OK",
                "post_inference": post,
            },
            rubric,
        )


def test_writer_validates_each_persisted_rubric_before_writing(tmp_path: Path) -> None:
    runner = _runner()
    post = _post(runner)
    valid = {
        "status": "OK",
        "post_inference": post,
        "manual_rubric": runner._unscored_rubric(),
    }
    runner._write(tmp_path, {"cases": [valid]})
    assert (tmp_path / "result.json").exists()

    malformed = {
        **valid,
        "manual_rubric": {
            "scoring_status": "SCORED",
            "reviewer_type": "HUMAN",
            "reviewer_identity": None,
            "scored_response_evidence_digest": "wrong",
            "scores": {dimension: 2 for dimension in runner.RUBRIC_DIMENSIONS},
            "notes": "",
        },
    }
    blocked = tmp_path / "blocked"
    with pytest.raises((TypeError, ValueError), match="digest"):
        runner._write(blocked, {"cases": [malformed]})
    assert not blocked.exists()

    scored = {
        **valid,
        "manual_rubric": {
            "scoring_status": "SCORED",
            "reviewer_type": "HUMAN",
            "reviewer_identity": None,
            "scored_response_evidence_digest": post["response_evidence_digest"],
            "scores": {dimension: 2 for dimension in runner.RUBRIC_DIMENSIONS},
            "notes": "Complete bounded review.",
        },
    }
    runner._write(tmp_path / "scored", {"cases": [scored]})
    assert (tmp_path / "scored" / "result.json").exists()


def test_writer_rejects_stale_response_evidence_without_replacing_existing_artifact(
    tmp_path: Path,
) -> None:
    runner = _runner()
    post = _post(runner)
    scored = {
        "status": "OK",
        "post_inference": post,
        "manual_rubric": {
            "scoring_status": "SCORED",
            "reviewer_type": "HUMAN",
            "reviewer_identity": None,
            "scored_response_evidence_digest": post["response_evidence_digest"],
            "scores": {dimension: 2 for dimension in runner.RUBRIC_DIMENSIONS},
            "notes": "Bounded review.",
        },
    }
    runner._write(tmp_path, {"cases": [scored], "summary": {"prior": True}})
    prior = (tmp_path / "result.json").read_bytes()
    stale = {**scored, "post_inference": {**post, "answer_bounded": "mutated"}}
    with pytest.raises(ValueError, match="stale"):
        runner._write(tmp_path, {"cases": [stale], "summary": {"invalid": True}})
    assert (tmp_path / "result.json").read_bytes() == prior


def test_scored_rubric_cannot_cross_bind_case_or_model() -> None:
    runner = _runner()
    left_post = _post(runner, case_id="case-a", model_id="QWEN3_4B", run_ordinal=1)
    rubric = {
        "scoring_status": "SCORED",
        "reviewer_type": "HUMAN",
        "reviewer_identity": None,
        "scored_response_evidence_digest": left_post["response_evidence_digest"],
        "scores": {dimension: 2 for dimension in runner.RUBRIC_DIMENSIONS},
        "notes": "Bounded review.",
    }
    for post in (
        _post(runner, case_id="case-a", model_id="QWEN3_14B"),
        _post(runner, case_id="case-b", model_id="QWEN3_4B"),
        _post(runner, case_id="case-a", model_id="QWEN3_4B", run_ordinal=2),
    ):
        with pytest.raises(ValueError, match="digest"):
            runner._validate_manual_rubric({"status": "OK", "post_inference": post}, rubric)


def test_runner_output_replaces_prior_evidence(tmp_path: Path):
    runner = _runner()
    runner._write(tmp_path, {"source_commit": "old", "summary": {"stale": True}})
    runner._write(tmp_path, {"source_commit": "new", "summary": {"fresh": True}})

    assert '"source_commit": "new"' in (tmp_path / "result.json").read_text()
    assert "old" not in (tmp_path / "report.md").read_text()
    assert not list(tmp_path.glob("*.tmp"))
