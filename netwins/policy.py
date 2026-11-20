"""
The trust decision, in code. The LLM explains; this decides what may happen next.

Action classes:
  auto_apply_eligible   p >= auto_p, tier A, snapshot fresh, change allow-listed and within step bounds
                        (still off unless `auto_apply_enabled`)
  propose               approve_p <= p, tier A or B: a human approves (CANAH's approval)
  gather_more_data      undecided: the interval still straddles a threshold at the run cap, or tier C for a
                        change that would otherwise go ahead
  reject                p clearly below approve_p (whatever the trust: rejecting is the safe direction),
                        a stale snapshot, out of the validated range

A question with no change ("does the requirement hold today?") proposes nothing, so its actions are
  met / not_met         the same rules as propose / reject
  gather_more_data      as above

Every rule that fired is listed in `reasons`.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

TIER_RANK = {"A": 3, "B": 2, "C": 1}


@dataclass
class Policy:
    auto_p: float = 0.99
    approve_p: float = 0.90
    max_snapshot_age_s: float = 3600.0
    auto_apply_enabled: bool = False
    allow_list: List[str] = field(default_factory=lambda: ["links.*.rate_mbps", "links.*.queue_pkts",
                                                            "demands.*.rate_mbps"])
    max_step_ratio: float = 2.0       # a set value may change by at most this factor

    @property
    def thresholds(self) -> List[float]:
        return [self.approve_p, self.auto_p]

    def decide(self, env: Dict[str, Any], change: Optional[Dict[str, Any]] = None,
               current: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        got = self._decide(env, change, current)
        if change is None and got["action"] in ("propose", "reject", "auto_apply_eligible"):
            stale = any("snapshot" in r for r in got["reasons"]) or env.get("out_of_range")
            action = "gather_more_data" if stale else ("not_met" if got["action"] == "reject" else "met")
            reasons = [r for r in got["reasons"] if "human approves" not in r and "automatic apply" not in r]
            if action == "met":
                reasons.append("no change was asked about: nothing to approve")
            return {"action": action, "reasons": reasons}
        return got

    def _decide(self, env: Dict[str, Any], change: Optional[Dict[str, Any]] = None,
                current: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        reasons: List[str] = []
        pm = env.get("p_meets_requirement") or {}
        p, interval = pm.get("p"), pm.get("interval")
        tier = env.get("trust_tier", "C")
        age = env.get("snapshot_age_s")

        if p is None:
            return {"action": "gather_more_data", "reasons": ["no requirement was stated, so there is nothing "
                                                              "to decide: add a requirement"]}
        if age is not None and age > self.max_snapshot_age_s:
            reasons.append(f"snapshot is {int(age)} s old (limit {int(self.max_snapshot_age_s)} s)")
            return {"action": "reject", "reasons": reasons + ["take a fresh snapshot and ask again"]}
        if env.get("out_of_range"):
            return {"action": "reject", "reasons": [f"outside the validated range: {env['out_of_range']}"]}
        lo, hi = interval or (p, p)
        # A change predicted to fail clearly is rejected whatever the trust: rejecting is the safe direction.
        # Trust only gates the decisions that let a change go ahead.
        bp = (env.get("p_meets_baseline") or {})
        b_hi = (bp.get("interval") or [None, None])[1]
        if change is not None and hi < self.approve_p and b_hi is not None and b_hi < self.approve_p:
            return {"action": "gather_more_data", "reasons": [
                f"p={p:.3f} with the change, but the requirements fail without it too (baseline p="
                f"{bp.get('p', 0):.3f}): the change is not what breaks them. Ask the operator which requirements "
                f"apply, or fix what fails today first"]}
        if hi < self.approve_p:
            return {"action": "reject", "reasons": [f"p={p:.3f} (95% interval {lo:.3f}-{hi:.3f}) is below "
                                                     f"{self.approve_p}" + (f" (trust tier {tier})" if tier != "A" else "")]}
        if tier == "C":
            reasons.append(f"trust tier C ({env.get('calibration_why', 'uncalibrated')}): the twin is not "
                           f"trusted enough to let a change go ahead")
            return {"action": "gather_more_data", "reasons": reasons}
        if lo < self.approve_p <= hi:
            return {"action": "gather_more_data", "reasons": [f"p={p:.3f}: its interval {lo:.3f}-{hi:.3f} still "
                                                               f"straddles {self.approve_p} at the run cap"]}
        if p >= self.auto_p and lo >= self.auto_p and tier == "A":
            ok, why = self._safe_change(change, current)
            if ok and self.auto_apply_enabled:
                return {"action": "auto_apply_eligible", "reasons": [f"p={p:.3f} >= {self.auto_p}, tier A", *why]}
            reasons += [f"p={p:.3f} >= {self.auto_p}, tier A", *why]
            if not self.auto_apply_enabled:
                reasons.append("automatic apply is off")
        reasons.append(f"p={p:.3f} (95% interval {lo:.3f}-{hi:.3f}) >= {self.approve_p}, tier {tier}: "
                       f"a human approves")
        return {"action": "propose", "reasons": reasons}

    def _safe_change(self, change, current):
        why = []
        for s in (change or {}).get("set", []):
            path = s["path"]
            if not any(fnmatch.fnmatch(path, pat) for pat in self.allow_list):
                return False, [f"{path} is not allow-listed for automatic apply"]
            old = (current or {}).get(path)
            new = s["value"]
            if isinstance(new, (int, float)) and old:
                ratio = max(new / old, old / new) if new else float("inf")
                if ratio > self.max_step_ratio:
                    return False, [f"{path}: {old} to {new} is a step beyond x{self.max_step_ratio}"]
        if (change or {}).get("events"):
            return False, ["changes that inject events are never applied automatically"]
        return True, why
