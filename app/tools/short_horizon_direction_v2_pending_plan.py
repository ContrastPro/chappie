"""Immutable, read-only binding for the pending V2 benchmark inventory."""
from __future__ import annotations
import hashlib, json
from dataclasses import asdict, dataclass

PLAN_DIGEST="1181f07cd9036af426e028be5bc9670e3c62c1614d10776cb1968a155b5f6bdd"
BTC_ONLY="FIT_BTC_ONLY_1H"
PENDING=("FIT_CAPITAL_FLOW_LOGISTIC_1H","FIT_TECHNICAL_LOGISTIC_1H","FIT_VOLUME_LOGISTIC_1H","FIT_MARKET_STRUCTURE_LOGISTIC_1H","FIT_FULL_LOGISTIC_1H","FIT_FULL_LOGISTIC_4H")
MAX_NEW_FITS=6
@dataclass(frozen=True)
class PendingPlan:
 plan_digest:str=PLAN_DIGEST; verified_prerequisite:str=BTC_ONLY; pending:tuple[str,...]=PENDING
 def __post_init__(self):
  if self.plan_digest!=PLAN_DIGEST or self.verified_prerequisite!=BTC_ONLY or self.pending!=PENDING or len(set(self.pending))!=6: raise ValueError("frozen pending plan mismatch")
 @property
 def digest(self): return hashlib.sha256(json.dumps(asdict(self),sort_keys=True,separators=(',',':')).encode()).hexdigest()
 def resolve(self, verified: set[str]):
  if verified != {BTC_ONLY}: raise ValueError("verified prerequisite mismatch")
  return {"reused":(BTC_ONLY,),"pending":self.pending,"max_new_fits":MAX_NEW_FITS}
PENDING_PLAN=PendingPlan()
