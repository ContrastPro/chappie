"""One fixed, non-authoritative V2 logistic smoke execution; no CLI broadening."""

from __future__ import annotations

import json
from pathlib import Path

from app.research.capital_flow.short_horizon_direction_v2_fit import (
    build_v2_logistic_binding,
    execute_v2_fit_unit,
)

SMOKE_ROOT = Path("E:/projects/chappie/data/research/audits/short_horizon_direction_v2_smoke")
FIXED_FOLD = "V2_WF1"
FIXED_HORIZON = "1h"
FIXED_MODEL = "logistic_baseline"


def smoke_binding():
    """Resolve the sole approved smoke science binding."""
    return build_v2_logistic_binding(
        experiment_identity="CHAPPIE_SHORT_HORIZON_MARKET_DIRECTION_V2/1.0.0",
        fold_identity=FIXED_FOLD,
        horizon=FIXED_HORIZON,
    )


def run() -> dict[str, object]:
    """Execute exactly one reviewed fit and return structural evidence only."""
    evidence = execute_v2_fit_unit(smoke_binding())
    return {
        "classification": "NON_AUTHORITATIVE_SMOKE_TEST",
        **evidence,
    }


def main() -> int:
    print(json.dumps(run(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
