"""
The Wi-Fi twin: access points bridged to a wired backbone with a server, stations around them, the
traffic between stations and the server, run in ns-3 (engine/wifi/netwins-wifi.cc).

  scenario (domain "wifi")
    topology   aps (x, y in metres, transmit power, channel, width), stations (x, y; optionally the access
               point they start on), radio (standard ax/ac/n, log-distance path loss: exponent and
               reference loss at 1 m), backbone (rate, delay)
    traffic    demands between a station and "server" (either way), UDP at a rate or TCP bulk
    events     ap_down / ap_up
    change     set values (move an AP or a station, power, channel, width, the path-loss exponent...),
               events, add or remove demands

  A station starts on the access point it names, or else the one it hears loudest (the same log-distance
  model). Access points on the same channel contend and interfere; a station whose access point fails
  re-associates with another on its channel after ns-3's missed-beacon timeout, keeping its address.

  per demand: throughput_mbps, delay_ms_mean, jitter_ms_mean, loss_ratio (FlowMonitor)

Not modelled: walls and multipath beyond the path-loss exponent, rate control other than ns-3's ideal
manager, roaming across channels, client vendor behaviour.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Literal, Optional, Tuple

import numpy as np
from pydantic import Field, field_validator

from .schema import SCHEMA_VERSIONS, Demand, SeedPolicy, SetValue, Uncertain, _Strict

LIMITS = {"aps": 12, "stations": 64, "demands": 64, "events": 20, "duration_s": (2.0, 60.0),
          "coord_m": (-500.0, 500.0), "tx_power_dbm": (0.0, 30.0), "path_loss_exponent": (1.6, 6.0),
          "reference_loss_db": (30.0, 60.0), "demand_rate_mbps": (0.01, 2000.0)}
CHANNELS = {20: (36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 149, 153, 157, 161),
            40: (38, 46, 54, 62, 102, 110, 151, 159), 80: (42, 58, 106, 155), 160: (50, 114)}
NOT_MODELLED = ("Free-space-like log-distance propagation (no walls, multipath or shadowing beyond the exponent); "
                "ns-3's ideal rate control; roaming only between access points on the same channel; no client "
                "vendor behaviour.")
FIDELITY = "packet-level (ns-3 802.11 with bridged access points, log-distance propagation)"


class AP(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    x: float
    y: float
    tx_power_dbm: float = 20.0
    channel: int = 36
    width_mhz: Literal[20, 40, 80, 160] = 20


class Station(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    x: float
    y: float
    ap: Optional[str] = None


class Radio(_Strict):
    standard: Literal["ax", "ac", "n"] = "ax"
    path_loss_exponent: float = 3.0
    reference_loss_db: float = 46.6777


class Backbone(_Strict):
    rate_mbps: float = 1000.0
    delay_ms: float = 0.5


class WTopology(_Strict):
    aps: List[AP]
    stations: List[Station]
    radio: Radio = Field(default_factory=Radio)
    backbone: Backbone = Field(default_factory=Backbone)


class WTraffic(_Strict):
    demands: List[Demand] = Field(default_factory=list)


class WEvent(_Strict):
    t: float
    type: Literal["ap_down", "ap_up"]
    target: str


class WRequirement(_Strict):
    demand: str
    kpi: Literal["throughput_mbps", "delay_ms_mean", "jitter_ms_mean", "loss_ratio"]
    op: Literal["<", "<=", ">", ">="]
    value: float


class WChange(_Strict):
    set: List[SetValue] = Field(default_factory=list)
    events: List[WEvent] = Field(default_factory=list)
    add_demands: List[Demand] = Field(default_factory=list)
    remove_demands: List[str] = Field(default_factory=list)


class WScenario(_Strict):
    schema_version: str = "1.0"
    domain: Literal["wifi"]
    snapshot_id: str = ""
    question: str = ""
    topology: WTopology
    traffic: WTraffic = Field(default_factory=WTraffic)
    events: List[WEvent] = Field(default_factory=list)
    change: Optional[WChange] = None
    duration_s: float = 10.0
    requirements: List[WRequirement] = Field(default_factory=list)
    uncertain_params: List[Uncertain] = Field(default_factory=list)
    seed_policy: SeedPolicy = Field(default_factory=lambda: SeedPolicy(runs=6, max_runs=24, batch=6))

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: str) -> str:
        if v not in SCHEMA_VERSIONS:
            raise ValueError(f"unknown schema_version {v!r}; this twin reads {SCHEMA_VERSIONS}")
        return v


SETTABLE = {"aps": {"x", "y", "tx_power_dbm", "channel", "width_mhz"},
            "stations": {"x", "y"},
            "demands": {"rate_mbps", "packet_bytes", "start_s", "stop_s"},
            "radio": {"path_loss_exponent", "reference_loss_db"}}
WHERE = {"aps": "topology", "stations": "topology", "radio": "topology", "backbone": "topology",
         "demands": "traffic"}


def resolve(data: Dict[str, Any], path: str):
    from .paths import resolve as _r
    return _r(data, path, SETTABLE, WHERE)


def set_path(data: Dict[str, Any], path: str, value: Any) -> None:
    obj, field = resolve(data, path)
    obj[field] = int(value) if field in ("channel", "width_mhz", "packet_bytes") else value


def clamp(path: str, v: float) -> float:
    field = path.rsplit(".", 1)[-1]
    rng = LIMITS.get("coord_m") if field in ("x", "y") else LIMITS.get(field)
    if isinstance(rng, tuple):
        return min(max(v, rng[0]), rng[1])
    return max(v, 0.0)


def problems(sc: WScenario) -> List[str]:
    out: List[str] = []
    t = sc.topology
    aps = {a.id: a for a in t.aps}
    stas = {s.id for s in t.stations}
    for key, items in (("aps", t.aps), ("stations", t.stations), ("demands", sc.traffic.demands)):
        if len(items) > LIMITS[key]:
            out.append(f"{len(items)} {key}: the cap is {LIMITS[key]}")
    if set(aps) & stas or "server" in set(aps) | stas:
        out.append("ids: access points, stations and \"server\" must all be different")

    def within(name, v, rng, what):
        if v is not None and not rng[0] <= v <= rng[1]:
            out.append(f"{what}: {name} {v} is outside the validated range {rng[0]}..{rng[1]}")
    lo, hi = LIMITS["duration_s"]
    if not lo <= sc.duration_s <= hi:
        out.append(f"duration_s {sc.duration_s} is outside {lo}..{hi}")
    for a in t.aps:
        within("x", a.x, LIMITS["coord_m"], f"ap {a.id}")
        within("y", a.y, LIMITS["coord_m"], f"ap {a.id}")
        within("tx_power_dbm", a.tx_power_dbm, LIMITS["tx_power_dbm"], f"ap {a.id}")
        if a.channel not in CHANNELS[a.width_mhz]:
            out.append(f"ap {a.id}: channel {a.channel} is not a {a.width_mhz} MHz channel in 5 GHz "
                       f"({list(CHANNELS[a.width_mhz])})")
    for s in t.stations:
        within("x", s.x, LIMITS["coord_m"], f"station {s.id}")
        within("y", s.y, LIMITS["coord_m"], f"station {s.id}")
        if s.ap is not None and s.ap not in aps:
            out.append(f"station {s.id}: no access point {s.ap!r}")
    within("path_loss_exponent", t.radio.path_loss_exponent, LIMITS["path_loss_exponent"], "radio")
    within("reference_loss_db", t.radio.reference_loss_db, LIMITS["reference_loss_db"], "radio")
    ends = stas | {"server"}
    demands = list(sc.traffic.demands) + list(sc.change.add_demands if sc.change else [])
    for d in demands:
        for end in (d.src, d.dst):
            if end not in ends:
                out.append(f"demand {d.id}: no station {end!r} (or \"server\")")
        if "server" not in (d.src, d.dst):
            out.append(f"demand {d.id}: one end must be \"server\" (station to station is not modelled)")
        within("rate_mbps", d.rate_mbps, LIMITS["demand_rate_mbps"], f"demand {d.id}")
        stop = d.stop_s if d.stop_s is not None else sc.duration_s
        if not 0 <= d.start_s < stop <= sc.duration_s:
            out.append(f"demand {d.id}: start {d.start_s} and stop {stop} must satisfy 0 <= start < stop <= duration")
    ids = {d.id for d in demands}
    for r in sc.requirements:
        if r.demand not in ids:
            out.append(f"requirement on {r.kpi}: no demand {r.demand!r}")
    for e in [*sc.events, *(sc.change.events if sc.change else [])]:
        if e.target not in aps:
            out.append(f"event: no access point {e.target!r}")
        if not 0 <= e.t < sc.duration_s:
            out.append(f"event at t={e.t} is outside the run (0..{sc.duration_s})")
    data = sc.model_dump()
    for u in sc.uncertain_params:
        try:
            resolve(data, u.path)
        except ValueError as e:
            out.append(f"uncertain parameter: {e}")
    for s in (sc.change.set if sc.change else []):
        try:
            resolve(data, s.path)
        except ValueError as e:
            out.append(f"change: {e}")
    if not demands:
        out.append("no demands: there is nothing to measure")
    return out


def baseline_and_changed(sc: WScenario) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    base = sc.model_dump(exclude={"change"})
    changed = copy.deepcopy(base)
    if sc.change:
        for s in sc.change.set:
            set_path(changed, s.path, s.value)
        changed["events"] = [*changed["events"], *[e.model_dump() for e in sc.change.events]]
        gone = set(sc.change.remove_demands)
        changed["traffic"]["demands"] = [d for d in changed["traffic"]["demands"] if d["id"] not in gone]
        changed["traffic"]["demands"] += [d.model_dump() for d in sc.change.add_demands]
    return base, changed


def new_entities(sc: WScenario) -> set:
    return {d.id for d in sc.change.add_demands} if sc.change else set()


def rx_dbm(ap: Dict[str, Any], st: Dict[str, Any], radio: Dict[str, Any]) -> float:
    d = max(1.0, math.hypot(ap["x"] - st["x"], ap["y"] - st["y"]))
    return ap.get("tx_power_dbm", 20.0) - radio.get("reference_loss_db", 46.6777) - 10 * radio.get("path_loss_exponent", 3.0) * math.log10(d)


def engine_view(member: Dict[str, Any]) -> Dict[str, Any]:
    """What netwins-wifi reads; every station on an access point (named, or the loudest it hears)."""
    t = member["topology"]
    radio = t.get("radio") or {}
    aps = {a["id"]: a for a in t["aps"]}
    stations = []
    for s in t["stations"]:
        s = dict(s)
        if not s.get("ap"):
            s["ap"] = max(aps.values(), key=lambda a: rx_dbm(a, s, radio))["id"]
        stations.append({k: v for k, v in s.items() if v is not None})
    clean = lambda xs: [{k: v for k, v in x.items() if v is not None} for x in xs]  # noqa: E731
    return {"aps": clean(t["aps"]), "stations": stations, "radio": radio, "backbone": t.get("backbone") or {},
            "demands": clean(member["traffic"]["demands"]), "events": clean(member.get("events", [])),
            "duration_s": member["duration_s"]}


class FakeWifiEngine:
    """For tests only: each station's rate from its SNR (a capped Shannon), access points sharing airtime
    among their stations, an access point that is down passing its stations to the loudest other on the
    same channel. Not a twin."""
    name = "fake-wifi"

    def __init__(self, noise: float = 0.02):
        self.noise = noise
        self.calls = 0

    def run(self, member: Dict[str, Any], seed: int) -> Dict[str, Any]:
        self.calls += 1
        rng = np.random.default_rng(seed)
        v = engine_view(member)
        aps = {a["id"]: a for a in v["aps"]}
        downs = {e["target"] for e in v["events"] if e["type"] == "ap_down"}
        on: Dict[str, str] = {}
        for s in v["stations"]:
            ap = s["ap"]
            if ap in downs:
                same = [a for a in aps.values() if a["id"] not in downs and a.get("channel") == aps[ap].get("channel")]
                ap = max(same, key=lambda a: rx_dbm(a, s, v["radio"]))["id"] if same else None
            on[s["id"]] = ap
        phy: Dict[str, float] = {}
        for s in v["stations"]:
            ap = on[s["id"]]
            if ap is None:
                phy[s["id"]] = 0.0
                continue
            width = aps[ap].get("width_mhz", 20)
            snr = rx_dbm(aps[ap], s, v["radio"]) - (-94 + 10 * math.log10(width / 20))
            phy[s["id"]] = 0.0 if snr < 2 else min(width * 6.0, width * math.log2(1 + 10 ** (snr / 10)) * 0.55)
        per_ap: Dict[str, List[str]] = {}
        for sid, ap in on.items():
            if ap:
                per_ap.setdefault(ap, []).append(sid)
        out = {"run": seed, "demands": {}}
        offered: Dict[str, float] = {}
        for d in v["demands"]:
            sid = d["dst"] if d["src"] == "server" else d["src"]
            offered[sid] = offered.get(sid, 0.0) + d.get("rate_mbps", 1.0)
        for d in v["demands"]:
            sid = d["dst"] if d["src"] == "server" else d["src"]
            ap = on.get(sid)
            if not ap or phy[sid] <= 0:
                out["demands"][d["id"]] = {"throughput_mbps": 0.0, "delay_ms_mean": None, "jitter_ms_mean": None, "loss_ratio": 1.0}
                continue
            airtime = sum(offered.get(x, 0) / max(phy[x], 1e-6) for x in per_ap[ap])     # share of the AP's time asked for
            share = min(1.0, 1.0 / airtime) if airtime > 0 else 1.0
            thr = d.get("rate_mbps", 1.0) * share * (1 + rng.normal(0, self.noise))
            loss = min(1.0, max(0.0, 1 - share + abs(rng.normal(0, self.noise * 0.05))))
            out["demands"][d["id"]] = {"throughput_mbps": max(thr, 0.0), "delay_ms_mean": 1.0 + 20 * min(airtime, 5),
                                       "jitter_ms_mean": 0.3, "loss_ratio": loss}
        return out


EXAMPLE = {
    "schema_version": "1.0", "domain": "wifi", "snapshot_id": "<the snapshot it came from>",
    "question": "if ap-1 fails at t=5, does the camera keep its loss under 1%?",
    "topology": {"aps": [{"id": "ap-1", "x": 0, "y": 0, "channel": 36}, {"id": "ap-2", "x": 30, "y": 0, "channel": 36}],
                 "stations": [{"id": "cam", "x": 8, "y": 3}, {"id": "laptop", "x": 25, "y": -4}],
                 "radio": {"standard": "ax", "path_loss_exponent": 3.0}},
    "traffic": {"demands": [{"id": "camera", "src": "cam", "dst": "server", "protocol": "udp", "rate_mbps": 8, "start_s": 1},
                            {"id": "web", "src": "server", "dst": "laptop", "protocol": "udp", "rate_mbps": 20, "start_s": 1}]},
    "change": {"events": [{"t": 5, "type": "ap_down", "target": "ap-1"}]},
    "duration_s": 12,
    "requirements": [{"demand": "camera", "kpi": "loss_ratio", "op": "<", "value": 0.01}],
    "uncertain_params": [{"path": "radio.path_loss_exponent", "dist": "uniform", "low": 2.8, "high": 3.4}],
    "seed_policy": {"runs": 6, "max_runs": 24, "batch": 6},
}
