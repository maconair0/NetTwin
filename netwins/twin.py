"""
`predict(scenario)`: the whole twin pipeline, in code, in order. The agent writes the scenario and reads
the envelope; it never sequences these steps.

  1 validate      schema, then semantics; problems go back to the agent to fix
  2 ensemble      paired members (baseline and changed on the same seeds and inputs) in batches, adaptive:
                  stop once p_meets is clear of the policy thresholds, or at max_runs
  3 aggregate     per demand and KPI: distributions and the paired effect
  4 calibrate     model error from the calibration store added to every member; conformal intervals;
                  trust tier
  5 envelope      everything the decision and the report need, with versions and a run id
  6 policy        the action class and its reasons
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional

import numpy as np
from pydantic import ValidationError

from . import aggregate
from .calibration import Calibration
from .policy import TIER_RANK, Policy
from . import domains
from .runner import ENGINE_VERSION, members, run_batch
from .domains import parse


class Twin:
    def __init__(self, engine: Any, calibration: Optional[Calibration] = None, policy: Optional[Policy] = None,
                 workers: int = 0, engines: Optional[Dict[str, Any]] = None):
        """`engine` runs the IP twin; `engines` the others by domain (made from the domain's own default,
        of the same kind as `engine`: a fake engine for tests brings fake engines for the others)."""
        self.engine = engine
        self.engines: Dict[str, Any] = dict(engines or {})
        self.calibration = calibration or Calibration()
        self.policy = policy or Policy()
        self.workers = workers
        self._cache: Dict[str, Dict[str, Any]] = {}

    def engine_for(self, domain: str) -> Any:
        if domain == "ip":
            return self.engine
        if domain not in self.engines:
            spec = "fake" if getattr(self.engine, "name", "") == "fake" else domains.default_spec()
            self.engines[domain] = domains.get(domain).engine(spec)
        return self.engines[domain]

    def validate(self, data: Any) -> Dict[str, Any]:
        try:
            sc = parse(data)
        except (ValidationError, ValueError) as e:
            return {"ok": False, "stage": "schema", "problems": _schema_errors(e)}
        found = domains.get(sc.domain).problems(sc)
        return {"ok": not found, "stage": "semantics", "problems": found, "domain": sc.domain}

    def predict(self, data: Any, snapshot_age_s: Optional[float] = None,
                trust_cap: Optional[str] = None, cap_why: Optional[List[str]] = None) -> Dict[str, Any]:
        """`trust_cap`: the best tier the snapshot allows (mirror.trust_caps): a twin built on assumed
        capacities is not trusted more than its inputs, however well calibrated."""
        v = self.validate(data)
        if not v["ok"]:
            return {"ok": False, **v}
        sc = parse(data)
        dom = domains.get(sc.domain)
        engine = self.engine_for(sc.domain)
        key = hashlib.sha256(json.dumps([sc.model_dump(), ENGINE_VERSION, dom.engine_version, getattr(engine, "name", ""), trust_cap],
                                        sort_keys=True, default=str).encode()).hexdigest()[:16]
        if key in self._cache:
            cached = dict(self._cache[key])
            cached["cached"] = True
            return cached

        t0 = time.time()
        paired = sc.change is not None
        sp = sc.seed_policy
        results: List[Dict[str, Any]] = []
        done = 0
        target = sp.runs
        while done < target:
            n = min(sp.batch, target - done)
            results += run_batch(engine, members(sc, done, n), paired, self.workers)
            done += n
            if done >= target and sp.adaptive and done < sp.max_runs and sc.requirements:
                if not aggregate.decided(aggregate.p_meets(results, sc.requirements), self.policy.thresholds):
                    target = min(sp.max_runs, done + sp.batch)
        summary = aggregate.summarise(results)
        raw = aggregate.p_meets(results, sc.requirements)
        # The baseline is judged only on what exists in it (a demand the change adds is not there yet).
        new = dom.new_entities(sc)
        before = [r for r in sc.requirements if r.demand not in new]
        base = aggregate.p_meets(results, before, "baseline") if paired and before else None

        cal = self._calibrated(results, sc)
        if trust_cap and TIER_RANK[trust_cap] < TIER_RANK[cal["tier"]]:
            cal["tier"] = trust_cap
            cal["why"] += "; capped at tier " + trust_cap + " by the snapshot: " + "; ".join(cap_why or [])
        # The interval the twin stands behind, per requirement: the ensemble's p50 widened by the conformal
        # 90% quantile of past residuals (no calibration data: none is given).
        for r in sc.requirements:
            entry = summary.get(r.demand, {}).get(r.kpi, {})
            stats = entry.get("changed")
            if stats:
                entry["calibrated_90"] = self.calibration.interval(sc.domain, r.kpi, stats["p50"])
        env = {
            "ok": True, "run_id": uuid.uuid4().hex[:12], "cache_key": key, "domain": sc.domain,
            "question": sc.question, "snapshot_id": sc.snapshot_id, "snapshot_age_s": snapshot_age_s,
            "members": done, "paired_baseline": paired, "seconds": round(time.time() - t0, 1),
            "prediction": summary,
            "p_meets_requirement": cal["p_meets"],
            "p_meets_uncalibrated": raw,
            "p_meets_baseline": base,
            "requirements": [r.model_dump() for r in sc.requirements],
            "change_ran": sc.change.model_dump(exclude_defaults=True) if sc.change else None,
            "uncertainty_sources": ["seed", *(["input"] if sc.uncertain_params else []),
                                    *(["model_error"] if cal["applied"] else [])],
            "calibration": cal["status"], "calibration_why": cal["why"], "trust_tier": cal["tier"],
            "fidelity_tier": dom.fidelity,
            "not_modelled": dom.not_modelled,
            "engine_versions": {**ENGINE_VERSION, "engine": dom.engine_version, "engine_kind": getattr(engine, "name", "?")},
        }
        current = {s.path: _current(sc, s.path, dom) for s in (sc.change.set if sc.change else [])}
        env["decision"] = self.policy.decide(env, sc.change.model_dump() if sc.change else None, current)
        self._cache[key] = env
        return env

    def _calibrated(self, results: List[Dict[str, Any]], sc) -> Dict[str, Any]:
        """p_meets with the model-error term: each member's requirement KPIs shifted by residuals drawn
        from the calibration store (as many draws as keep it stable). The trust tier is the weakest of
        the KPIs the requirements use."""
        kpis = sorted({r.kpi for r in sc.requirements})
        status = {k: self.calibration.status(sc.domain, k) for k in kpis}
        tier = min((s["trust_tier"] for s in status.values()), key=lambda t: TIER_RANK[t], default="C")
        why = "; ".join(f"{k}: {s['why']}" for k, s in status.items()) or "no requirement"
        res = {k: self.calibration.residuals(sc.domain, k) for k in kpis}
        raw = aggregate.p_meets(results, sc.requirements)
        if not sc.requirements or not any(r.size for r in res.values()):
            return {"p_meets": raw, "status": status, "tier": tier, "why": why, "applied": False}
        rng = np.random.default_rng(0)
        members = list(aggregate.by_side(results)["changed"].values())
        draws = 50
        k = 0
        for m in members:
            for _ in range(draws):
                shifted = {d: dict(v) for d, v in m.items()}
                for r in sc.requirements:
                    rr = res[r.kpi]
                    if rr.size and shifted.get(r.demand, {}).get(r.kpi) is not None:
                        shifted[r.demand][r.kpi] += float(rng.choice(rr))
                k += aggregate.meets(shifted, sc.requirements)
        n = len(members) * draws
        lo, hi = aggregate.wilson(round(k / draws), len(members))   # interval at the members' sample size
        return {"p_meets": {"p": k / n, "k": round(k / draws), "n": len(members), "interval": [lo, hi],
                            "model_error_draws": draws},
                "status": status, "tier": tier, "why": why, "applied": True}


def _current(sc, path: str, dom: Any):
    obj, field = dom.resolve(sc.model_dump(), path)
    return obj.get(field)


def _schema_errors(e: Exception) -> List[str]:
    if isinstance(e, ValidationError):
        return [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
    return [str(e)]


def compact(env: Dict[str, Any]) -> Dict[str, Any]:
    """What the agent needs from an envelope: the decision, the probabilities, the figures behind each
    requirement, the trust and what is not modelled. The full envelope is kept under its run id."""
    if not env.get("ok"):
        return env
    figures = {}
    for r in env.get("requirements", []):
        p = env["prediction"].get(r["demand"], {}).get(r["kpi"], {})
        figures[f"{r['demand']}.{r['kpi']}"] = {
            k: ([round(x, 6) for x in v] if isinstance(v, list) else
                ({kk: round(vv, 6) for kk, vv in v.items() if kk in ("mean", "p5", "p50", "p95")} if v else None))
            for k, v in p.items()}
    return {"run_id": env["run_id"], "decision": env["decision"], "p_meets_requirement": env["p_meets_requirement"],
            "p_meets_uncalibrated": env["p_meets_uncalibrated"], "p_meets_baseline": env["p_meets_baseline"],
            "requirements": env["requirements"],
            "change_ran (exactly what was simulated; say only this)": env.get("change_ran"),
            "figures (changed / baseline / effect / calibrated_90)": figures,
            "members": env["members"], "paired_baseline": env["paired_baseline"], "trust_tier": env["trust_tier"],
            "calibration": env["calibration_why"], "snapshot": [env["snapshot_id"], env["snapshot_age_s"]],
            "fidelity_tier": env["fidelity_tier"], "not_modelled": env["not_modelled"],
            "engine": env["engine_versions"], "cached": env.get("cached", False)}
