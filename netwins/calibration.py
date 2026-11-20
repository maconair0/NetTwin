"""
How wrong the twin has been, per domain and KPI, and what that does to a prediction.

Store (SQLite): one row per (prediction, later measurement): domain, kpi, the predicted p5/p50/p95,
the measured value, its source ("testbed" or "synthetic"), when.

  interval(domain, kpi, p50)   split-conformal: p50 ± q, q the ceil((n+1)(1-alpha))/n quantile of the
                               absolute residuals |measured - p50|. Finite-sample coverage >= 1-alpha
                               when residuals are exchangeable.
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


class Calibration:
    def __init__(self, path: Optional[Path] = None):
        self._db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def add(self, domain: str, kpi: str, predicted: Dict[str, float], measured: float, source: str,
            features: str = "", at: Optional[float] = None) -> None:
        if source not in ("testbed", "synthetic"):
            raise ValueError("source is testbed or synthetic")
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

    def q(self, domain: str, kpi: str, alpha: float = 0.1) -> Optional[float]:
        r = np.abs(self.residuals(domain, kpi))
        n = r.size
        if n == 0:
            return None
        level = math.ceil((n + 1) * (1 - alpha)) / n
        if level > 1:
            return float("inf")              # too few points for this alpha: no finite interval yet
        return float(np.quantile(r, level, method="higher"))

    def interval(self, domain: str, kpi: str, p50: float, alpha: float = 0.1) -> Optional[List[float]]:
        q = self.q(domain, kpi, alpha)
        return None if q is None else [p50 - q, p50 + q]

    def loo_coverage(self, domain: str, kpi: str, alpha: float = 0.1) -> Optional[float]:
        """How often a past measurement fell inside the conformal interval built from all the *other* past
        measurements (leave-one-out): the coverage of what the twin actually reports."""
        r = np.abs(self.residuals(domain, kpi))
        n = r.size
        if n < 3:
            return None
        inside = 0
        for i in range(n):
            others = np.delete(r, i)
            level = math.ceil((others.size + 1) * (1 - alpha)) / others.size
            q = float(np.quantile(others, min(level, 1.0), method="higher")) if level <= 1 else float("inf")
            inside += r[i] <= q
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
        return {"n": n, "coverage_raw_p5_p95": round(raw, 3),
                "coverage_conformal_90": None if conformal is None else round(conformal, 3),
                "conformal_q90": self.q(domain, kpi, 0.1), "newest": newest, "sources": sources,
                "trust_tier": tier, "why": why}

    def close(self) -> None:
        with self._lock:
            self._db.close()
