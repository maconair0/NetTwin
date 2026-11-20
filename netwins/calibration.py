"""
How wrong the twin has been, per domain and KPI, and what that does to a prediction.

Store (SQLite): one row per (prediction, later measurement): domain, kpi, the predicted p5/p50/p95,
the measured value, its source ("testbed" or "synthetic"), when.

  interval(domain, kpi, p50)   split-conformal around the bias-corrected prediction: p50 + b ± q, b the
                               median of past residuals (measured - p50: a model that is consistently low
                               or high is moved, not merely widened), q the ceil((n+1)(1-alpha))/n quantile
                               of |residual - b|. Finite-sample coverage >= 1-alpha when residuals are
                               exchangeable.
  residuals(domain, kpi)       signed residuals, to add to each member (the model-error term in p_meets)
  status(domain, kpi)          n; the raw ensemble's coverage (how often its p5-p95 held the measurement:
                               seed spread alone is usually far too narrow); the leave-one-out coverage
                               of the conformal 90% interval, which is what the twin reports; and the
                               trust tier, judged on the conformal coverage:
                                 A  n >= 30, coverage >= 0.85, newest within 30 days, not synthetic only
                                 B  n >= 10, coverage >= 0.80
                                 C  otherwise (uncalibrated): it may reject, never let a change go ahead
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

_SCHEMA = """create table if not exists residuals (
  id integer primary key, domain text, kpi text, p5 real, p50 real, p95 real, measured real,
  source text, features text, at real);
create index if not exists residuals_dk on residuals(domain, kpi);"""


# testbed: measured on a real testbed. lab: worked out by the lab's own physics (the emulated lab's devices
# report what it says, so for this lab it is the ground truth; it is still a model, and is labelled so).
# synthetic: the twin against a perturbed copy of itself (never enough for tier A).
SOURCES = ("testbed", "lab", "synthetic")


class Calibration:
    def __init__(self, path: Optional[Path] = None):
        self._db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def add(self, domain: str, kpi: str, predicted: Dict[str, float], measured: float, source: str,
            features: str = "", at: Optional[float] = None) -> None:
        if source not in SOURCES:
            raise ValueError(f"source is one of {SOURCES}")
        with self._lock:
            self._db.execute("insert into residuals (domain, kpi, p5, p50, p95, measured, source, features, at) "
                             "values (?,?,?,?,?,?,?,?,?)",
                             (domain, kpi, predicted["p5"], predicted["p50"], predicted["p95"], measured, source,
                              features, at or time.time()))

    def _rows(self, domain: str, kpi: str) -> List[tuple]:
        with self._lock:
            return self._db.execute("select p5, p50, p95, measured, source, at from residuals "
                                    "where domain = ? and kpi = ?", (domain, kpi)).fetchall()

    def residuals(self, domain: str, kpi: str) -> np.ndarray:
        return np.asarray([m - p50 for _p5, p50, _p95, m, _s, _a in self._rows(domain, kpi)], dtype=float)

    def bias(self, domain: str, kpi: str) -> float:
        r = self.residuals(domain, kpi)
        return float(np.median(r)) if r.size else 0.0

    def q(self, domain: str, kpi: str, alpha: float = 0.1) -> Optional[float]:
        raw = self.residuals(domain, kpi)
        r = np.abs(raw - (np.median(raw) if raw.size else 0.0))
        n = r.size
        if n == 0:
            return None
        level = math.ceil((n + 1) * (1 - alpha)) / n
        if level > 1:
            return float("inf")              # too few points for this alpha: no finite interval yet
        return float(np.quantile(r, level, method="higher"))

    def interval(self, domain: str, kpi: str, p50: float, alpha: float = 0.1) -> Optional[List[float]]:
        q = self.q(domain, kpi, alpha)
        if q is None:
            return None
        b = self.bias(domain, kpi)
        return [p50 + b - q, p50 + b + q]

    def loo_coverage(self, domain: str, kpi: str, alpha: float = 0.1) -> Optional[float]:
        """How often a past measurement fell inside the conformal interval built from all the *other* past
        measurements (leave-one-out): the coverage of what the twin actually reports."""
        raw = self.residuals(domain, kpi)
        n = raw.size
        if n < 3:
            return None
        inside = 0
        for i in range(n):
            others = np.delete(raw, i)
            b = float(np.median(others))
            spread = np.abs(others - b)
            level = math.ceil((others.size + 1) * (1 - alpha)) / others.size
            q = float(np.quantile(spread, min(level, 1.0), method="higher")) if level <= 1 else float("inf")
            inside += abs(raw[i] - b) <= q
        return inside / n

    def status(self, domain: str, kpi: str) -> Dict[str, Any]:
        rows = self._rows(domain, kpi)
        n = len(rows)
        if not n:
            return {"n": 0, "trust_tier": "C", "why": "no calibration data"}
        raw = sum(1 for p5, _p50, p95, m, _s, _a in rows if p5 <= m <= p95) / n
        conformal = self.loo_coverage(domain, kpi)
        newest = max(a for *_x, a in rows)
        sources = sorted({s for *_x, s, _a in rows})
        synthetic_only = sources == ["synthetic"]
        fresh = time.time() - newest < 30 * 86400
        cov = conformal if conformal is not None else 0.0
        if n >= 30 and cov >= 0.85 and fresh and not synthetic_only:
            tier, why = "A", "calibrated on measurements"
        elif n >= 10 and cov >= 0.80:
            tier = "B"
            why = ("calibrated on synthetic data only (never tier A)" if synthetic_only
                   else "calibrated, below tier-A bounds")
        else:
            tier, why = "C", f"n={n}, calibrated coverage {cov:.2f}: not enough to trust"
        if tier == "A" and "lab" in sources and "testbed" not in sources:
            why = "calibrated on the lab's physics (its emulated devices' ground truth), not on hardware"
        return {"n": n, "coverage_raw_p5_p95": round(raw, 3),
                "coverage_conformal_90": None if conformal is None else round(float(conformal), 3),
                "conformal_q90": self.q(domain, kpi, 0.1), "bias": round(self.bias(domain, kpi), 4),
                "newest": newest, "sources": sources,
                "trust_tier": tier, "why": why}

    def close(self) -> None:
        with self._lock:
            self._db.close()
