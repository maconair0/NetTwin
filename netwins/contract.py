"""
netwins behind the `canah.twin/1` contract: the four calls any agent (CANAH, or anything else) makes of a
digital twin, answered in the contract's shapes. In-process it is this module's `twin()`; over HTTP it is
`python -m netwins serve` (netwins/service.py), the same object behind four routes.

  describe()                          what netwins is: its twins (ip, optical, wifi), what each answers, its
                                      KPIs, a worked example, its fidelity and what it does not model
  validate(scenario)                  {"ok", "problems"}
  predict(scenario)                   the result: decision, probability, trust, figures, the change that ran
  check_write(controller, tool,       a controller write tried in the twin: the live network mirrored through
              arguments, answers)     the agentic MCP, the write translated into a change, predicted, decided

Settings (environment):
  NETWINS_HOME        the calibration store and saved runs (default ./netwins_data)
  NETWINS_ENGINE      docker (default) | local:<binary> | fake (tests)
  NETWINS_SNAPSHOTS   the snapshots folder
  AGENTIC_MCP_URL, AGENTIC_MCP_TOKEN   where check_write reads the controllers (the agentic MCP)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import domains, mirror, state
from .calibration import Calibration
from .twin import Twin

CONTRACT = "canah.twin/1"
VERSION = "0.4"
ANSWERS = {
    "ip": "What a failure, a capacity change or new traffic does to each demand's throughput, delay, jitter and loss "
          "in a packet network.",
    "optical": "Whether lightpaths keep their margin (GSNR, OSNR, pre-FEC BER) after a fibre cut, a longer or lossier "
               "span, a faster mode or a new lightpath, and what that does to the traffic they carry.",
    "wifi": "What an access point failing, a move, transmit power, channel or width, or more traffic does to each "
            "station's throughput, delay and loss, roaming included.",
}
KPIS = {"ip": ["throughput_mbps", "delay_ms_mean", "jitter_ms_mean", "loss_ratio"],
        "optical": ["gsnr_db", "osnr_db", "margin_db", "pre_fec_ber", "up", "path_km", "hops",
                    "throughput_mbps", "loss_ratio", "delay_ms_mean"],
        "wifi": ["throughput_mbps", "delay_ms_mean", "jitter_ms_mean", "loss_ratio"]}


def _stats(s: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    return None if not s else {k: s[k] for k in ("mean", "p5", "p50", "p95") if s.get(k) is not None}


def to_result(env: Dict[str, Any]) -> Dict[str, Any]:
    """A netwins envelope in the contract's Result shape."""
    if not env.get("ok"):
        return {"contract": CONTRACT, "ok": False, "problems": env.get("problems") or ["the scenario could not run"]}
    reqs = env.get("requirements") or []
    pairs = [(r["demand"], r["kpi"]) for r in reqs] or [(e, k) for e, ks in env["prediction"].items() for k in ks]
    figures = []
    for entity, kpi in dict.fromkeys(pairs):
        f = (env["prediction"].get(entity) or {}).get(kpi) or {}
        figures.append({"entity": entity, "kpi": kpi, "changed": _stats(f.get("changed")), "baseline": _stats(f.get("baseline")),
                        "effect": _stats(f.get("effect")), "calibrated_90": f.get("calibrated_90")})
    pm, pb = env.get("p_meets_requirement") or {}, env.get("p_meets_baseline") or None
    dec = env.get("decision") or {}
    return {
        "contract": CONTRACT, "ok": True,
        "twin": {"name": "netwins", "version": VERSION, "engine": (env.get("engine_versions") or {}).get("engine", "")},
        "domain": env.get("domain", ""), "run_id": env.get("run_id", ""), "question": env.get("question", ""),
        "decision": {"action": dec.get("action", "gather_more_data"), "reasons": dec.get("reasons", [])},
        "probability": {"p": pm.get("p"), "interval": pm.get("interval"), "members": pm.get("n")} if pm else None,
        "baseline_probability": {"p": pb.get("p"), "interval": pb.get("interval"), "members": pb.get("n")} if pb else None,
        "trust": {"tier": env.get("trust_tier", "C"), "why": env.get("calibration_why", "")},
        "figures": figures, "requirements": reqs, "change_ran": env.get("change_ran"),
        "not_modelled": env.get("not_modelled", ""), "fidelity": env.get("fidelity_tier", ""),
        "seconds": env.get("seconds"), "cached": bool(env.get("cached")),
    }


class NetwinsTwin:
    def __init__(self, home: Optional[str] = None, engine: Optional[str] = None, source: Any = None):
        self.home = Path(home or os.getenv("NETWINS_HOME", "netwins_data"))
        self.home.mkdir(parents=True, exist_ok=True)
        spec = engine or domains.default_spec()
        self.twin = Twin(domains.get("ip").engine(spec), Calibration(self.home / "calibration.sqlite"))
        self._source = source
        self._checker = None

    # -- the contract
    def describe(self) -> Dict[str, Any]:
        out = []
        for name in domains.names():
            d = domains.get(name)
            out.append({"domain": name, "title": d.title, "answers": ANSWERS.get(name, ""), "kpis": KPIS.get(name, []),
                        "example": d.example, "fidelity": d.fidelity, "not_modelled": d.not_modelled,
                        "engine": d.engine_version, "skill": d.skill})
        return {"contract": CONTRACT, "name": "netwins", "version": VERSION,
                "about": "ns-3 and GNPy digital twins of IP, optical and Wi-Fi networks, with calibrated trust and a "
                         "deterministic decision policy",
                "domains": out, "capabilities": ["validate", "predict", "check_write", "extras"],
                "controller_domains": ["packet", "ip"], "extras": EXTRAS}

    def extra(self, name: str, arguments: Dict[str, Any]) -> Any:
        """netwins' read-only helpers: snapshots of the networks, and the slice to build a scenario from."""
        a = arguments or {}
        if name == "snapshots":
            return [{"network": n, "snapshot_id": sid} for n, sid in state.domains().items()]
        if name == "inspect":
            return state.inspect(str(a.get("network") or "ip"))
        if name == "subnetwork":
            anchors = a.get("anchors") or []
            if isinstance(anchors, str):
                anchors = [x.strip() for x in anchors.split(",") if x.strip()]
            return state.subnetwork(str(a.get("network") or "ip"), list(anchors), int(a.get("hops", 2)))
        if name == "mirror":
            return self.mirror(str(a.get("controller") or ""))
        raise KeyError(f"netwins has no extra {name!r}; it has {[e['name'] for e in EXTRAS]}")

    def validate(self, scenario: Any) -> Dict[str, Any]:
        v = self.twin.validate(scenario)
        return {"ok": bool(v["ok"]), "problems": list(v.get("problems") or [])}

    def predict(self, scenario: Any) -> Dict[str, Any]:
        snap = None
        try:
            sid = (scenario or {}).get("snapshot_id") if isinstance(scenario, dict) else None
            snap = state.get(sid) if sid else None
        except Exception:  # noqa: BLE001
            snap = None
        cap, why = mirror.trust_caps(snap) if snap else (None, [])
        env = self.twin.predict(scenario, snapshot_age_s=state.age_s(snap) if snap else None, trust_cap=cap, cap_why=why)
        if env.get("ok"):
            runs = self.home / "runs"
            runs.mkdir(exist_ok=True)
            import json
            (runs / f"{env['run_id']}.json").write_text(json.dumps(env, default=str))
        return to_result(env)

    def check_write(self, controller: str, tool: str, arguments: Dict[str, Any], answers: Dict[str, Any]) -> Dict[str, Any]:
        from . import gate
        if self._checker is None:
            self._checker = gate.Checker(self.twin, self._source, runs_dir=self.home / "runs",
                                         interval_s=float(os.getenv("NETWINS_MIRROR_INTERVAL_S", "5")))
        rec = self._checker.check(controller, tool, arguments or {}, answers or {})
        s = gate.summary(rec)
        return {"check_id": rec["check_id"], "controller": controller, "tool": rec.get("tool", tool),
                "action": rec["action"], "reasons": rec.get("reasons") or [], "questions": rec.get("questions") or [],
                "next": rec.get("next", ""), "send_as": s.get("send_as"), "arguments": rec.get("arguments") or {},
                "result": to_result(rec["envelope"]) if rec.get("envelope") else None,
                "change_ran": rec.get("change"), "write_tools": rec.get("write_tools")}

    # -- what else netwins can do for an agent that knows it (not part of the contract)
    def mirror(self, controller: str) -> Dict[str, Any]:
        from . import gate
        if self._checker is None:
            self._checker = gate.Checker(self.twin, self._source, runs_dir=self.home / "runs",
                                         interval_s=float(os.getenv("NETWINS_MIRROR_INTERVAL_S", "5")))
        snap = self._checker.snapshot(controller, refresh=True)
        return {"snapshot_id": snap["snapshot_id"], "coverage": snap["coverage"], "provenance": snap["provenance"]}


EXTRAS = [
    {"name": "snapshots", "description": "the networks netwins has a snapshot of (mirrored from a controller, or saved)",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "inspect", "description": "a network's latest snapshot, summarised: nodes, links (rate, delay), demands, its age",
     "parameters": {"type": "object", "properties": {"network": {"type": "string"}}, "required": ["network"]}},
    {"name": "subnetwork", "description": "the slice of a network around some nodes or links (anchors), with every demand "
     "that crosses it: the topology and traffic to copy into a scenario, and its snapshot_id",
     "parameters": {"type": "object", "properties": {"network": {"type": "string"}, "anchors": {"type": "array", "items": {"type": "string"}},
                                                       "hops": {"type": "integer", "default": 2}}, "required": ["network"]}},
    {"name": "mirror", "description": "read a controller's network now (through the agentic MCP) into a fresh snapshot: what "
     "was measured and what had to be assumed",
     "parameters": {"type": "object", "properties": {"controller": {"type": "string"}}, "required": ["controller"]}},
]

_TWIN: Optional[NetwinsTwin] = None


def twin() -> NetwinsTwin:
    """The netwins twin of this process (CANAH's registry spec: "netwins.contract:twin")."""
    global _TWIN
    if _TWIN is None:
        _TWIN = NetwinsTwin()
    return _TWIN
