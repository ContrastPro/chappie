"""Read-only structural audit for one V2 Research fold; never fits or predicts."""

from __future__ import annotations

import argparse
import json

from app.research.capital_flow.tsunami_fold_engine import (
    PreprocessingV1,
    load_v2_fold_dataset,
    preprocessing_binding_digest,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon", choices=("1h", "4h"), required=True)
    parser.add_argument("--preprocess", action="store_true")
    args = parser.parse_args()
    train = load_v2_fold_dataset("V2_WF1", "TRAIN", args.horizon)
    assessment = load_v2_fold_dataset("V2_WF1", "ASSESSMENT", args.horizon)
    payload = {
        "fold": "V2_WF1",
        "horizon": args.horizon,
        "train_rows": len(train.timestamps),
        "assessment_rows": len(assessment.timestamps),
        "feature_width": len(train.values[0]),
        "state_width": len(train.states[0]),
        "chronological": train.timestamps == tuple(sorted(train.timestamps))
        and assessment.timestamps == tuple(sorted(assessment.timestamps)),
        "research_only": max(train.timestamps + assessment.timestamps).isoformat()
        < "2025-06-01T00:00:00+00:00",
    }
    if args.preprocess:
        indexes = tuple(range(60))
        preprocessing = PreprocessingV1.fit(train, indexes)
        train_matrix = preprocessing.transform(train, indexes)
        assessment_matrix = preprocessing.transform(assessment, indexes)
        payload["preprocessing"] = {
            "train_shape": list(train_matrix.shape),
            "assessment_shape": list(assessment_matrix.shape),
            "binding_digest": preprocessing_binding_digest(train, indexes),
            "assessment_isolation": True,
        }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
