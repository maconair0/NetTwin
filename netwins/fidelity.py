"""
A synthetic ground truth, to build and test the calibration pipeline before testbed measurements exist.

For each case: the twin predicts a scenario (its ensemble's p5/p50/p95 per demand and KPI), and "reality" is
one run of the same scenario with hidden perturbations the twin is not told about: link delays up to +15%,
rates up to -10%, a little random loss, a different seed. The residual goes into the calibration store with
source "synthetic". It exercises the pipeline; it says nothing about fidelity to a real network, and the
store never lets synthetic data earn trust tier A.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

import numpy as np

from . import aggregate, state
from .calibration import Calibration
from .runner import members, run_batch
from .schema import parse

KPIS = ("throughput_mbps", "delay_ms_mean", "loss_ratio")


def perturb(member: Dict[str, Any], rng: np.random.Generator) -> Dict[str, Any]:
    m = copy.deepcopy(member)
    for l in m["topology"]["links"]:
        l["delay_ms"] = l["delay_ms"] * rng.uniform(1.0, 1.15)
        l["rate_mbps"] = l["rate_mbps"] * rng.uniform(0.90, 1.0)
        l["loss_rate"] = min(0.5, l.get("loss_rate", 0.0) + rng.uniform(0, 0.002))
    return m


def cases(domain: str, n: int, rng: np.random.Generator) -> List[Dict[str, Any]]:
    """n variations of the domain's snapshot: scaled demand rates, a random link failing (or not)."""
    snap = state.latest(domain)
    if snap is None:
        raise ValueError(f"no snapshot of {domain}")
    out = []
    for i in range(n):
        sc = {"schema_version": "1.0", "domain": domain, "snapshot_id": snap["snapshot_id"],
              "topology": copy.deepcopy(snap["topology"]), "traffic": copy.deepcopy(snap["traffic"]),
              "events": [], "duration_s": 20, "seed_policy": {"runs": 10, "max_runs": 10, "adaptive": False,
                                                                "base_seed": 1000 + i}}
        for d in sc["traffic"]["demands"]:
            d["stop_s"] = min(d.get("stop_s") or 20, 20)
            if d.get("protocol", "udp") == "udp":
                d["rate_mbps"] = round(d["rate_mbps"] * rng.uniform(0.5, 1.6), 2)
        if rng.random() < 0.5:
            link = rng.choice([l["id"] for l in sc["topology"]["links"]])
            sc["events"].append({"t": float(rng.uniform(4, 12)), "type": "link_down", "target": str(link)})
        out.append(sc)
    return out


def calibrate(engine: Any, cal: Calibration, domain: str = "ip", n: int = 30, seed: int = 7,
              workers: int = 0) -> Dict[str, Any]:
    rng = np.random.default_rng(seed)
    added = 0
    for case in cases(domain, n, rng):
        sc = parse(case)
        results = run_batch(engine, members(sc, 0, sc.seed_policy.runs), paired=False, workers=workers)
        summary = aggregate.summarise(results)
        truth_member = perturb(members(sc, 10_000, 1)[0][2], rng)
        truth = engine.run(truth_member, int(rng.integers(50_000, 60_000)))["demands"]
        for demand, kpis in summary.items():
            for k in KPIS:
                pred = (kpis.get(k) or {}).get("changed")
                measured = (truth.get(demand) or {}).get(k)
                if pred and measured is not None:
                    cal.add(domain, k, pred, float(measured), "synthetic", features=sc.snapshot_id)
                    added += 1
    return {"added": added, "status": {k: cal.status(domain, k) for k in KPIS}}
