from __future__ import annotations
import pytest
from app.tools.short_horizon_direction_v2_pending_plan import BTC_ONLY,MAX_NEW_FITS,PENDING,PENDING_PLAN,PendingPlan,PLAN_DIGEST
def test_frozen_pending_plan():
 assert PENDING_PLAN.plan_digest==PLAN_DIGEST and len(PENDING_PLAN.pending)==6 and MAX_NEW_FITS==6
 assert PENDING_PLAN.resolve({BTC_ONLY})["reused"]==(BTC_ONLY,)
 assert PENDING_PLAN.digest==PendingPlan().digest
def test_rejects_deviation():
 with pytest.raises(ValueError): PendingPlan(pending=tuple(reversed(PENDING)))
 with pytest.raises(ValueError): PENDING_PLAN.resolve(set())
