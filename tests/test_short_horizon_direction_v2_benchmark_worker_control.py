from __future__ import annotations

import ast
from pathlib import Path

from app.tools import short_horizon_direction_v2_benchmark_worker_control as control


def test_control_entrypoint_is_operational_and_wrapper_bound():
    source = Path(control.__file__).read_text(encoding="utf-8")
    assert control.CONTROL_IDENTITY == "CHAPPIE_V2_BENCHMARK_WORKER_CONTROL/1.0.0"
    assert "run_worker" in source
    forbidden = (
        "load_v2_fold_dataset",
        "fit_probability_model",
        "predict_probability_model",
        "feature_store",
        "target_authority",
        "--model",
        "--fold",
        "--horizon",
    )
    assert not any(item in source for item in forbidden)
    ast.parse(source)
