"""
From member results to a prediction: distributions, requirements, paired effects, and when to stop.

  summarise    per demand and KPI: n, mean, std, p5/p50/p95 (changed side), the baseline's, and the paired
               effect (changed - baseline, member by member)
  p_meets      the fraction of members meeting every requirement, with its Wilson 95% interval
  decided      whether p_meets is clear of the policy thresholds (adaptive stopping)
"""
from __future__ import annotations

import math
import operator
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

KPIS = ("throughput_mbps", "delay_ms_mean", "jitter_ms_mean", "loss_ratio")
OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


def _stats(values: List[float]) -> Optional[Dict[str, float]]:
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return None
    a = np.asarray(vals, dtype=float)
    return {"n": int(a.size), "mean": float(a.mean()), "std": float(a.std(ddof=1)) if a.size > 1 else 0.0,
            "p5": float(np.percentile(a, 5)), "p50": float(np.percentile(a, 50)), "p95": float(np.percentile(a, 95))}


def by_side(results: Iterable[Dict[str, Any]]) -> Dict[str, Dict[int, Dict[str, Any]]]:
    out: Dict[str, Dict[int, Dict[str, Any]]] = {"changed": {}, "baseline": {}}
    for r in results:
        out[r["side"]][r["seed"]] = r["result"]["demands"]
    return out


def summarise(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    sides = by_side(results)
    seeds = sorted(sides["changed"])
    demands = sorted({d for m in sides["changed"].values() for d in m})
    out: Dict[str, Dict[str, Any]] = {}
    for d in demands:
        out[d] = {}
        for k in KPIS:
            changed = [sides["changed"][s].get(d, {}).get(k) for s in seeds]
            entry = {"changed": _stats(changed)}
            if sides["baseline"]:
                base = [sides["baseline"].get(s, {}).get(d, {}).get(k) for s in seeds]
                entry["baseline"] = _stats(base)
                deltas = [c - b for c, b in zip(changed, base) if c is not None and b is not None]
                entry["effect"] = _stats(deltas)
            out[d][k] = entry
    return out


def meets(member: Dict[str, Any], requirements: Sequence[Any]) -> bool:
    for r in requirements:
        v = (member.get(r.demand) or {}).get(r.kpi)
        if v is None or not OPS[r.op](v, r.value):
            return False
    return True


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def p_meets(results: List[Dict[str, Any]], requirements: Sequence[Any], side: str = "changed") -> Dict[str, Any]:
    members = list(by_side(results)[side].values())
    if not requirements:
        return {"p": None, "k": 0, "n": len(members), "interval": None}
    k = sum(meets(m, requirements) for m in members)
    lo, hi = wilson(k, len(members))
    return {"p": k / len(members) if members else None, "k": k, "n": len(members), "interval": [lo, hi]}


def decided(pm: Dict[str, Any], thresholds: Sequence[float]) -> bool:
    """True when the Wilson interval does not straddle any policy threshold: more runs would not change the
    action class."""
    if pm.get("interval") is None:
        return True
    lo, hi = pm["interval"]
    return not any(lo < t <= hi for t in thresholds)
