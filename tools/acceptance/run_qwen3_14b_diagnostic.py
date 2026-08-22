"""Run one bounded, local-only qwen3:14b diagnostic without mutating Chappie."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from app.knowledge.store import committed_snapshot, read_store
from app.platform.bootstrap import ChappiePaths, collect_status, load_local_config

OUTPUT_RELATIVE = Path("data/acceptance/model_ab/diagnostic")
RAW_TIMEOUT_SECONDS = 180.0
CHAPPIE_TIMEOUT_SECONDS = 90.0
QUESTION = "Understand architecture"
MODELS = ("qwen3:4b", "qwen3:14b")
ALLOWED_DIRTY_PATHS = frozenset(
    {
        "tools/acceptance/run_qwen3_14b_diagnostic.py",
        "tools/acceptance/run_model_ab.py",
        "tests/test_qwen3_14b_diagnostic.py",
        "tests/test_model_ab_runner.py",
        "tools/acceptance/README.md",
        "docs/architecture/MULTI_MODEL_FOUNDATION.md",
    }
)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as value:
        value.bind(("127.0.0.1", 0))
        return int(value.getsockname()[1])


def _write(output: Path, result: dict[str, object]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "report.md").write_text(_report(result), encoding="utf-8")


def _report(result: dict[str, object]) -> str:
    preflight = result.get("preflight", {})
    tests = result.get("tests", {})
    timing = result.get("timing_ms", {})
    lines = [
        "# Chappie qwen3:14b diagnostic",
        "",
        "Generated local diagnostic evidence; not canonical Knowledge.",
        "",
        "## Environment",
        f"- Source commit: {result.get('source_commit')}",
        f"- Preflight: {preflight.get('status', 'PENDING') if isinstance(preflight, dict) else 'PENDING'}",
        f"- Default model: {result.get('default_model')}",
        "",
        "## Preflight",
    ]
    checks = preflight.get("checks", []) if isinstance(preflight, dict) else []
    for check in checks if isinstance(checks, list) else []:
        if isinstance(check, dict):
            detail = f" — {check['message']}" if check.get("message") else ""
            lines.append(f"- {check.get('name')}: {check.get('status')}{detail}")
    failures = preflight.get("failures", []) if isinstance(preflight, dict) else []
    if failures:
        lines.append("- Failures:")
        for failure in failures if isinstance(failures, list) else []:
            if isinstance(failure, dict):
                suffix = f" ({failure['path']})" if failure.get("path") else ""
                lines.append(f"  - {failure.get('code')}: {failure.get('message')}{suffix}")
    lines.extend(
        (
            "",
            "## Raw Ollama",
        )
    )
    for key in ("raw_4b", "raw_14b"):
        item = tests.get(key, {}) if isinstance(tests, dict) else {}
        lines.append(
            f"- {key}: {item.get('status', 'PENDING') if isinstance(item, dict) else 'PENDING'}"
        )
    lines.extend(("", "## Chappie Project QA"))
    for key in ("chappie_default", "chappie_4b", "chappie_14b", "default_after_14b"):
        item = tests.get(key, {}) if isinstance(tests, dict) else {}
        if isinstance(item, dict):
            lines.append(
                f"- {key}: {item.get('status', 'PENDING')} "
                f"({item.get('effective_model_id') or 'no executed model'})"
            )
    lines.extend(
        (
            "",
            "## Model-state isolation",
            f"- Default after 14B: {result.get('default_after_14b_isolated')}",
            "",
            "## Timing",
            f"- {json.dumps(timing, sort_keys=True)}",
            "",
            "## Diagnosis",
            f"- Primary: {result.get('diagnosis', 'PENDING')}",
            f"- Secondary: {result.get('secondary_diagnosis')}",
            "",
            "## Recommended next step",
            f"- {result.get('recommended_next_step', 'Review result.json and this report.')}",
        )
    )
    return "\n".join(lines) + "\n"


def _classify(result: dict[str, object]) -> tuple[str, str | None]:
    tests = result.get("tests", {})
    if not isinstance(tests, dict):
        return "DIAGNOSTIC_ENVIRONMENT_FAILURE", None
    raw14 = tests.get("raw_14b", {})
    chappie14 = tests.get("chappie_14b", {})
    after = tests.get("default_after_14b", {})
    if not isinstance(raw14, dict) or not isinstance(chappie14, dict):
        return "DIAGNOSTIC_ENVIRONMENT_FAILURE", None
    if raw14.get("status") != "OK":
        return "RAW_14B_FAILURE", None
    if chappie14.get("status") == "TIMEOUT":
        return "CHAPPIE_14B_TIMEOUT", (
            "PROVEN_TIMEOUT_MISMATCH" if int(raw14.get("elapsed_ms") or 0) > 45_000 else None
        )
    if chappie14.get("status") == "PROVIDER_ERROR":
        return "CHAPPIE_14B_PROVIDER_FAILURE", None
    if chappie14.get("status") == "VALIDATION_ERROR":
        return "CHAPPIE_14B_STRUCTURED_RESPONSE_FAILURE", None
    if chappie14.get("status") != "OK":
        return "CHAPPIE_14B_LIFECYCLE_FAILURE", None
    if isinstance(after, dict) and after.get("effective_model_id") != "QWEN3_4B":
        return "MULTI_MODEL_SELECTION_FAILURE", "MULTI_MODEL_STATE_LEAK"
    return "QWEN3_14B_WORKS", None


def _raw_model(model: str) -> dict[str, object]:
    started = time.monotonic()
    try:
        response = httpx.post(
            "http://127.0.0.1:11434/api/chat",
            json={
                "model": model,
                "stream": False,
                "think": False,
                "messages": [{"role": "user", "content": "Reply with exactly READY"}],
            },
            timeout=RAW_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload.get("message", {}).get("content", "") if isinstance(payload, dict) else ""
        return {
            "status": "OK",
            "model": str(payload.get("model", model)) if isinstance(payload, dict) else model,
            "response_summary": str(content)[:200],
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": None,
        }
    except (httpx.HTTPError, ValueError) as error:
        return {
            "status": "TIMEOUT" if isinstance(error, httpx.TimeoutException) else "PROVIDER_ERROR",
            "model": model,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": type(error).__name__,
        }


def _project_qa(client: httpx.Client, requested_model_id: str | None) -> dict[str, object]:
    started = time.monotonic()
    body: dict[str, object] = {"question": QUESTION}
    if requested_model_id is not None:
        body["requested_model_id"] = requested_model_id
    try:
        response = client.post("/v1/project/qa", json=body, timeout=CHAPPIE_TIMEOUT_SECONDS)
        payload: Any = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return {
                "status": "TRANSPORT_ERROR",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "error": error
                if isinstance(error, dict)
                else {"http_status": response.status_code},
            }
        return {
            "status": str(data.get("state", "UNKNOWN")),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "requested_model_id": data.get("requested_model_id"),
            "effective_model_id": data.get("effective_model_id"),
            "effective_model": data.get("effective_model"),
            "provider": data.get("provider"),
            "answer_length": len(data.get("answer") or ""),
            "sources_count": len(data.get("supporting_records") or []),
            "error": data.get("error"),
        }
    except httpx.TimeoutException:
        return {
            "status": "TIMEOUT",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": "OUTER_TIMEOUT",
        }
    except (httpx.HTTPError, ValueError) as error:
        return {
            "status": "TRANSPORT_ERROR",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": type(error).__name__,
        }


def _dirty_paths(root: Path) -> set[str]:
    output = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return {line[3:] for line in output if len(line) >= 4}


def _tree_check(dirty_paths: set[str]) -> tuple[dict[str, object], list[dict[str, str]]]:
    unexpected = sorted(dirty_paths.difference(ALLOWED_DIRTY_PATHS))
    if not unexpected:
        return (
            {
                "name": "production tree validity",
                "status": "PASS",
                "message": "clean or limited to dedicated diagnostic tooling",
            },
            [],
        )
    return (
        {"name": "production tree validity", "status": "FAIL", "message": "unexpected dirty path"},
        [
            {
                "code": "UNEXPECTED_DIRTY_PATH",
                "message": "production provenance may be stale",
                "path": path,
            }
            for path in unexpected
        ],
    )


def _preflight(paths: ChappiePaths) -> tuple[dict[str, object], list[dict[str, str]]]:
    failures: list[dict[str, str]] = []
    checks: list[dict[str, object]] = []
    snapshot = committed_snapshot(paths.root)
    status = collect_status(paths)
    config = load_local_config(paths)
    try:
        records, manifest = read_store(paths.knowledge_file)
        knowledge = (
            bool(records)
            and manifest.snapshot.commit == snapshot.commit
            and manifest.snapshot.clean
        )
    except ValueError:
        knowledge = False
    try:
        tags = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2.0).json()
        installed = {item.get("name") for item in tags.get("models", []) if isinstance(item, dict)}
        ollama_reachable = True
    except (httpx.HTTPError, ValueError, AttributeError):
        installed = set()
        ollama_reachable = False
    tree_check, tree_failures = _tree_check(_dirty_paths(paths.root))
    checks.append(tree_check)
    failures.extend(tree_failures)
    checks.append(
        {"name": "Knowledge READY and verified", "status": "PASS" if knowledge else "FAIL"}
    )
    if not knowledge:
        failures.append(
            {"code": "KNOWLEDGE_NOT_READY", "message": "canonical Knowledge is absent or stale"}
        )
    checks.append({"name": "Ollama reachable", "status": "PASS" if ollama_reachable else "FAIL"})
    if not ollama_reachable:
        failures.append(
            {"code": "OLLAMA_NOT_READY", "message": "local Ollama tags endpoint is unavailable"}
        )
    default_valid = config is not None and config.model == "qwen3:4b"
    checks.append({"name": "Configured default", "status": "PASS" if default_valid else "FAIL"})
    if config is None or config.model != "qwen3:4b":
        failures.append(
            {"code": "DEFAULT_CONFIG_INVALID", "message": "expected durable default qwen3:4b"}
        )
    for model in MODELS:
        present = model in installed
        checks.append({"name": f"{model} installed", "status": "PASS" if present else "FAIL"})
        if model not in installed:
            failures.append(
                {
                    "code": "MODEL_MISSING",
                    "message": "required local model is absent",
                    "path": model,
                }
            )
    required = {item.component: item.state.value for item in status.components if item.required}
    if required.get("AI_PROVIDER") != "READY":
        failures.append(
            {"code": "AI_PROVIDER_NOT_READY", "message": "Chappie provider status is not READY"}
        )
    return (
        {
            "status": "OK" if not failures else "FAILED",
            "snapshot_commit": snapshot.commit,
            "knowledge_ready": knowledge,
            "default_model": config.model if config else None,
            "installed_models": sorted(item for item in installed if isinstance(item, str)),
            "checks": checks,
            "failures": failures,
        },
        failures,
    )


def main() -> int:
    paths = ChappiePaths.resolve()
    output = paths.root / OUTPUT_RELATIVE
    preflight, failures = _preflight(paths)
    result: dict[str, object] = {
        "source_commit": committed_snapshot(paths.root).commit,
        "preflight": preflight,
        "preflight_failures": failures,
        "default_model": preflight.get("default_model"),
        "tests": {},
        "timing_ms": {},
    }
    _write(output, result)
    if failures:
        result["diagnosis"] = "DIAGNOSTIC_ENVIRONMENT_FAILURE"
        result["recommended_next_step"] = "Resolve the listed preflight failures, then rerun."
        _write(output, result)
        return 0
    log = (output / "control_plane.log").open("w", encoding="utf-8")
    port = _free_loopback_port()
    child = subprocess.Popen(
        [sys.executable, "-m", "app.cli", "serve", "--port", str(port)],
        cwd=paths.root,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            for _ in range(40):
                try:
                    if client.get("/v1/health", timeout=1.0).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                result["diagnosis"] = "DIAGNOSTIC_ENVIRONMENT_FAILURE"
                result["recommended_next_step"] = "Review control_plane.log."
                _write(output, result)
                return 0
            tests: dict[str, object] = result["tests"]  # type: ignore[assignment]
            tests["raw_4b"] = _raw_model("qwen3:4b")
            _write(output, result)
            tests["raw_14b"] = _raw_model("qwen3:14b")
            _write(output, result)
            tests["chappie_default"] = _project_qa(client, None)
            _write(output, result)
            tests["chappie_4b"] = _project_qa(client, "QWEN3_4B")
            _write(output, result)
            tests["chappie_14b"] = _project_qa(client, "QWEN3_14B")
            _write(output, result)
            tests["default_after_14b"] = _project_qa(client, None)
            result["default_after_14b_isolated"] = (
                isinstance(tests["default_after_14b"], dict)
                and tests["default_after_14b"].get("effective_model_id") == "QWEN3_4B"
            )
            try:
                observed = subprocess.run(
                    ["ollama", "ps"], capture_output=True, text=True, timeout=5, check=False
                )
                result["ollama_ps"] = observed.stdout[:2000]
            except (OSError, subprocess.SubprocessError):
                result["ollama_ps"] = None
            result["timing_ms"] = {
                name: item.get("elapsed_ms")
                for name, item in tests.items()
                if isinstance(item, dict)
            }
            result["diagnosis"], result["secondary_diagnosis"] = _classify(result)
            result["recommended_next_step"] = (
                "Review this report before changing production behavior."
            )
            _write(output, result)
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
