"""
The optical twin: lightpaths over fibres, their quality of transmission from GNPy, and the packets they
carry.

  scenario (domain "optical")
    topology   nodes (ROADM sites), links (fibres: length, loss per km, span length, connector loss)
    traffic    lightpaths (src, dst, rate 100/200/300/400 G, an optional fixed path of fibres, restore on
               a cut or not) and packet demands riding them (lightpath, rate)
    events     link_down / link_up on fibres (a cut, a repair)
    change     set values, events, add or remove lightpaths

  engine (OpticalEngine, per member)
    1. the fibres' state after the events; each lightpath on its path, rerouted on the shortest surviving
       path when a fibre of it is cut and it restores, down otherwise
    2. GNPy designs the line (amplifiers every span, its power settings) and propagates a full C-band
       load: the lightpath's GSNR and OSNR (0.1 nm), its margin over its mode's required OSNR, its pre-FEC
       BER from the SNR in its own bandwidth, its length
    3. the packet layer: a demand rides its lightpath at the lightpath's capacity while the margin is
       positive (past the FEC cliff it carries nothing), shared in proportion when oversubscribed;
       delay is the fibre's (4.9 us per km)

  per entity (the `demand` of a requirement names either):
    lightpath  gsnr_db, osnr_db, margin_db, pre_fec_ber, up, path_km, hops
    demand     throughput_mbps, loss_ratio, delay_ms_mean

QoT is static: no transients, no power excursions after a cut, no wavelength assignment (a full load is
assumed, the worst case for nonlinear noise). The ensemble varies the uncertain physical parameters
(fibre loss, connector loss, span length...), so p_meets is over what is not known exactly about the
plant, calibrated like every twin.
"""
from __future__ import annotations

import copy
import heapq
import logging
import math
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import Field, field_validator

from .schema import SCHEMA_VERSIONS, SeedPolicy, SetValue, Uncertain, _Strict

LIMITS = {"nodes": 100, "links": 200, "lightpaths": 100, "demands": 200, "length_km": (1.0, 6000.0),
          "loss_db_per_km": (0.14, 0.40), "span_km": (10.0, 150.0), "con_loss_db": (0.0, 3.0),
          "tx_power_dbm": (-8.0, 6.0), "demand_rate_mbps": (0.001, 400_000.0)}
NOT_MODELLED = ("Static quality of transmission (GNPy, full C-band load): no transients or power excursions after a "
                "cut, no wavelength assignment or spectrum continuity, amplifiers placed by GNPy's design rules, "
                "not your line system's; the packet layer is a fluid model with a hard FEC cliff.")
FIDELITY = "physical layer: GNPy GN model per lightpath; packet layer: fluid, capacity and FEC cliff"
KPIS_LIGHTPATH = ("gsnr_db", "osnr_db", "margin_db", "pre_fec_ber", "up", "path_km", "hops")
KPIS_DEMAND = ("throughput_mbps", "loss_ratio", "delay_ms_mean")

# GNPy's Voyager transceiver (its example equipment library): rate -> (mode, baud GBd, required OSNR dB 0.1 nm)
MODES = {100: ("mode 1", 32.0, 12.0), 200: ("mode 4", 66.0, 16.0), 300: ("mode 3", 44.0, 18.0),
         400: ("mode 2", 66.0, 21.0)}


class ONode(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    kind: Literal["roadm", "terminal"] = "roadm"


class Fibre(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    a: str
    z: str
    length_km: float
    loss_db_per_km: float = 0.2
    span_km: float = 80.0          # an amplifier at most every span_km
    con_loss_db: float = 0.5       # per span end
    fiber_type: Literal["SSMF", "NZDF"] = "SSMF"


class OTopology(_Strict):
    nodes: List[ONode]
    links: List[Fibre]


class Lightpath(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    src: str
    dst: str
    rate_gbps: Literal[100, 200, 300, 400] = 100
    path: Optional[List[str]] = None       # fibre ids, src to dst; none: the shortest by length
    restore: bool = True                   # rerouted on the shortest surviving path when cut
    tx_power_dbm: Optional[float] = None   # per channel into each span; none: GNPy's design


class ODemand(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    lightpath: str
    rate_mbps: float


class OTraffic(_Strict):
    lightpaths: List[Lightpath] = Field(default_factory=list)
    demands: List[ODemand] = Field(default_factory=list)


class OEvent(_Strict):
    t: float = 0.0
    type: Literal["link_down", "link_up"]
    target: str


class ORequirement(_Strict):
    demand: str                    # a lightpath or a packet demand
    kpi: Literal["gsnr_db", "osnr_db", "margin_db", "pre_fec_ber", "up", "path_km", "hops",
                 "throughput_mbps", "loss_ratio", "delay_ms_mean"]
    op: Literal["<", "<=", ">", ">="]
    value: float


class OChange(_Strict):
    set: List[SetValue] = Field(default_factory=list)
    events: List[OEvent] = Field(default_factory=list)
    add_lightpaths: List[Lightpath] = Field(default_factory=list)
    remove_lightpaths: List[str] = Field(default_factory=list)


class OScenario(_Strict):
    schema_version: str = "1.0"
    domain: Literal["optical"]
    snapshot_id: str = ""
    question: str = ""
    topology: OTopology
    traffic: OTraffic = Field(default_factory=OTraffic)
    events: List[OEvent] = Field(default_factory=list)
    change: Optional[OChange] = None
    duration_s: float = 1.0        # static QoT: kept for the shared pipeline
    requirements: List[ORequirement] = Field(default_factory=list)
    uncertain_params: List[Uncertain] = Field(default_factory=list)
    seed_policy: SeedPolicy = Field(default_factory=lambda: SeedPolicy(runs=10, max_runs=40, batch=10))

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: str) -> str:
        if v not in SCHEMA_VERSIONS:
            raise ValueError(f"unknown schema_version {v!r}; this twin reads {SCHEMA_VERSIONS}")
        return v


SETTABLE = {"links": {"length_km", "loss_db_per_km", "span_km", "con_loss_db"},
            "lightpaths": {"tx_power_dbm", "rate_gbps"},
            "demands": {"rate_mbps"}}
WHERE = {"links": "topology", "nodes": "topology", "lightpaths": "traffic", "demands": "traffic"}


def problems(sc: OScenario) -> List[str]:
    out: List[str] = []
    nodes = {n.id for n in sc.topology.nodes}
    fibres = {l.id: l for l in sc.topology.links}
    for key, items in (("nodes", sc.topology.nodes), ("links", sc.topology.links),
                       ("lightpaths", sc.traffic.lightpaths), ("demands", sc.traffic.demands)):
        if len(items) > LIMITS[key]:
            out.append(f"{len(items)} {key}: the cap is {LIMITS[key]}")
        ids = [x.id for x in items]
        if len(ids) != len(set(ids)):
            out.append(f"{key}: ids repeat")

    def within(name, v, rng, what):
        if v is not None and not rng[0] <= v <= rng[1]:
            out.append(f"{what}: {name} {v} is outside the validated range {rng[0]}..{rng[1]}")
    for l in sc.topology.links:
        for end in (l.a, l.z):
            if end not in nodes:
                out.append(f"fibre {l.id}: no node {end!r}")
        within("length_km", l.length_km, LIMITS["length_km"], f"fibre {l.id}")
        within("loss_db_per_km", l.loss_db_per_km, LIMITS["loss_db_per_km"], f"fibre {l.id}")
        within("span_km", l.span_km, LIMITS["span_km"], f"fibre {l.id}")
        within("con_loss_db", l.con_loss_db, LIMITS["con_loss_db"], f"fibre {l.id}")
    lps = list(sc.traffic.lightpaths) + list(sc.change.add_lightpaths if sc.change else [])
    for lp in lps:
        for end in (lp.src, lp.dst):
            if end not in nodes:
                out.append(f"lightpath {lp.id}: no node {end!r}")
        if lp.src == lp.dst:
            out.append(f"lightpath {lp.id}: its ends are the same node")
        within("tx_power_dbm", lp.tx_power_dbm, LIMITS["tx_power_dbm"], f"lightpath {lp.id}")
        if lp.path:
            at = lp.src
            for fid in lp.path:
                f = fibres.get(fid)
                if f is None:
                    out.append(f"lightpath {lp.id}: no fibre {fid!r} on its path")
                    break
                if at not in (f.a, f.z):
                    out.append(f"lightpath {lp.id}: its path is not continuous at {fid} (it is at {at})")
                    break
                at = f.z if at == f.a else f.a
            else:
                if at != lp.dst:
                    out.append(f"lightpath {lp.id}: its path ends at {at}, not {lp.dst}")
    lp_ids = {lp.id for lp in lps}
    for d in sc.traffic.demands:
        if d.lightpath not in lp_ids:
            out.append(f"demand {d.id}: no lightpath {d.lightpath!r}")
        within("rate_mbps", d.rate_mbps, LIMITS["demand_rate_mbps"], f"demand {d.id}")
    entities = lp_ids | {d.id for d in sc.traffic.demands}
    for r in sc.requirements:
        if r.demand not in entities:
            out.append(f"requirement on {r.kpi}: no lightpath or demand {r.demand!r}")
        elif r.demand in lp_ids and r.kpi not in KPIS_LIGHTPATH:
            out.append(f"requirement on {r.demand}: a lightpath has {list(KPIS_LIGHTPATH)}, not {r.kpi}")
        elif r.demand not in lp_ids and r.kpi not in KPIS_DEMAND:
            out.append(f"requirement on {r.demand}: a demand has {list(KPIS_DEMAND)}, not {r.kpi}")
    for e in [*sc.events, *(sc.change.events if sc.change else [])]:
        if e.target not in fibres:
            out.append(f"event: no fibre {e.target!r}")
    if sc.change:
        for rid in sc.change.remove_lightpaths:
            if rid not in {lp.id for lp in sc.traffic.lightpaths}:
                out.append(f"change: no lightpath {rid!r} to remove")
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
    if not lps:
        out.append("no lightpaths: there is nothing to predict")
    return out


def resolve(data: Dict[str, Any], path: str):
    from .paths import resolve as _r
    return _r(data, path, SETTABLE, WHERE)


def set_path(data: Dict[str, Any], path: str, value: Any) -> None:
    obj, field = resolve(data, path)
    obj[field] = int(value) if field == "rate_gbps" else value


def clamp(path: str, v: float) -> float:
    field = path.rsplit(".", 1)[-1]
    lo, hi = LIMITS.get(field, (0.0, float("inf"))) if isinstance(LIMITS.get(field), tuple) else (0.0, float("inf"))
    return min(max(v, lo), hi)


def baseline_and_changed(sc: OScenario) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    base = sc.model_dump(exclude={"change"})
    changed = copy.deepcopy(base)
    if sc.change:
        for s in sc.change.set:
            set_path(changed, s.path, s.value)
        changed["events"] = [*changed["events"], *[e.model_dump() for e in sc.change.events]]
        gone = set(sc.change.remove_lightpaths)
        changed["traffic"]["lightpaths"] = [lp for lp in changed["traffic"]["lightpaths"] if lp["id"] not in gone]
        changed["traffic"]["lightpaths"] += [lp.model_dump() for lp in sc.change.add_lightpaths]
    return base, changed


def new_entities(sc: OScenario) -> set:
    return {lp.id for lp in sc.change.add_lightpaths} if sc.change else set()


# ----------------------------------------------------------------------------- the engine

def _ber(snr_db: float, bits_per_symbol: float) -> float:
    """Pre-FEC BER from the SNR in the signal's own bandwidth (Gray-coded, AWGN)."""
    snr = 10 ** (snr_db / 10)
    if bits_per_symbol <= 2.2:                                   # DP-QPSK
        return 0.5 * math.erfc(math.sqrt(snr / 2))
    if bits_per_symbol <= 3.2:                                   # DP-8QAM (approximation)
        return (2 / 3) * math.erfc(math.sqrt(3 * snr / 14))
    return (3 / 8) * math.erfc(math.sqrt(snr / 10))              # DP-16QAM


class OpticalEngine:
    """GNPy for each lightpath's QoT, a fluid packet layer on top. Deterministic per member: the ensemble
    varies only what the scenario declares uncertain."""
    name = "gnpy"

    def __init__(self):
        self._eq = None
        self._memo: Dict[Any, Dict[str, float]] = {}

    def _equipment(self):
        if self._eq is None:
            import gnpy
            from pathlib import Path
            from gnpy.core.parameters import SimParams
            from gnpy.tools.json_io import load_equipments_and_configs
            logging.getLogger("gnpy").setLevel(logging.ERROR)
            self._eq = load_equipments_and_configs(Path(gnpy.__file__).parent / "example-data" / "eqpt_config.json", [], [])
            SimParams.set_params({})
        return self._eq

    @staticmethod
    def version() -> str:
        try:
            import importlib.metadata as m
            return f"gnpy {m.version('gnpy')}"
        except Exception:  # noqa: BLE001
            return "gnpy ?"

    def qot(self, hops: List[Tuple[str, str, Dict[str, Any]]], lp: Dict[str, Any]) -> Dict[str, float]:
        """GNPy along one route: hops [(from node, to node, fibre)]."""
        key = (tuple((a, b, f["length_km"], f["loss_db_per_km"], f["span_km"], f["con_loss_db"], f["fiber_type"])
                     for a, b, f in hops), lp.get("tx_power_dbm"), lp["rate_gbps"])
        if key in self._memo:
            return self._memo[key]
        import numpy as np
        from gnpy.tools.json_io import network_from_json
        from gnpy.tools.worker_utils import designed_network, transmission_simulation
        eq = self._equipment()
        src, dst = hops[0][0], hops[-1][1]
        el = [{"uid": f"trx {src}", "type": "Transceiver"}, {"uid": f"trx {dst}", "type": "Transceiver"}]
        conn = [{"from_node": f"trx {src}", "to_node": f"roadm {src}"}, {"from_node": f"roadm {dst}", "to_node": f"trx {dst}"}]
        roadms = {src, dst} | {b for _, b, _ in hops[:-1]}
        el += [{"uid": f"roadm {r}", "type": "Roadm"} for r in roadms]
        for i, (a, b, f) in enumerate(hops):
            n = max(1, math.ceil(f["length_km"] / f["span_km"] - 1e-9))
            prev = f"roadm {a}"
            for k in range(n):
                uid = f"fibre {i}.{k}"
                el.append({"uid": uid, "type": "Fiber", "type_variety": f["fiber_type"],
                           "params": {"length": f["length_km"] / n, "length_units": "km",
                                      "loss_coef": f["loss_db_per_km"], "con_in": f["con_loss_db"], "con_out": 0.0}})
                conn.append({"from_node": prev, "to_node": uid})
                prev = uid
            conn.append({"from_node": prev, "to_node": f"roadm {b}"})
        net = network_from_json({"elements": el, "connections": conn}, eq)
        power = lp.get("tx_power_dbm")
        net, req, ref = designed_network(eq, net, f"trx {src}", f"trx {dst}", nodes_list=[f"trx {dst}"],
                                         loose_list=["STRICT"], args_power=0 if power is None else power)
        path, *_ = transmission_simulation(eq, net, req, ref)
        end = path[-1]
        mid = len(end.snr_01nm) // 2
        gsnr = float(np.asarray(end.snr_01nm)[mid])
        osnr = float(np.asarray(end.osnr_ase_01nm)[mid]) if hasattr(end, "osnr_ase_01nm") else float(np.asarray(end.osnr_ase)[mid])
        _mode, baud, need = MODES[int(lp["rate_gbps"])]
        snr_sig = gsnr - 10 * math.log10(baud / 12.5)
        bits = lp["rate_gbps"] / baud / 2
        got = {"gsnr_db": round(gsnr, 3), "osnr_db": round(osnr, 3), "margin_db": round(gsnr - need, 3),
               "pre_fec_ber": _ber(snr_sig, bits)}
        if len(self._memo) < 20000:
            self._memo[key] = got
        return got

    def run(self, member: Dict[str, Any], seed: int) -> Dict[str, Any]:
        fibres = {f["id"]: f for f in member["topology"]["links"]}
        up = {fid: True for fid in fibres}
        for e in sorted(member.get("events", []), key=lambda e: e.get("t", 0)):
            if e["target"] in up:
                up[e["target"]] = e["type"] == "link_up"
        out: Dict[str, Any] = {"run": seed, "demands": {}, "routes": {}}
        cap: Dict[str, float] = {}
        delay: Dict[str, float] = {}
        for lp in member["traffic"]["lightpaths"]:
            route = None
            if lp.get("path") and all(up.get(f, False) for f in lp["path"]):
                route = list(lp["path"])
            elif not lp.get("path") or lp.get("restore", True):
                route = _shortest(fibres, up, lp["src"], lp["dst"])
            if route is None:
                out["demands"][lp["id"]] = {"gsnr_db": None, "osnr_db": None, "margin_db": None, "pre_fec_ber": None,
                                            "up": 0, "path_km": None, "hops": None}
                cap[lp["id"]], delay[lp["id"]] = 0.0, None
                out["routes"][lp["id"]] = None
                continue
            hops, at = [], lp["src"]
            for fid in route:
                f = fibres[fid]
                nxt = f["z"] if at == f["a"] else f["a"]
                hops.append((at, nxt, f))
                at = nxt
            q = self.qot(hops, lp)
            km = sum(f["length_km"] for _, _, f in hops)
            alive = q["margin_db"] >= 0
            out["demands"][lp["id"]] = {**q, "up": 1 if alive else 0, "path_km": round(km, 3), "hops": len(hops)}
            out["routes"][lp["id"]] = route
            cap[lp["id"]] = lp["rate_gbps"] * 1000.0 if alive else 0.0
            delay[lp["id"]] = km * 0.0049
        load: Dict[str, float] = {}
        for d in member["traffic"].get("demands", []):
            load[d["lightpath"]] = load.get(d["lightpath"], 0.0) + d["rate_mbps"]
        for d in member["traffic"].get("demands", []):
            c, lp = cap.get(d["lightpath"], 0.0), d["lightpath"]
            share = 0.0 if c <= 0 else min(1.0, c / max(load[lp], 1e-9))
            out["demands"][d["id"]] = {"throughput_mbps": d["rate_mbps"] * share, "loss_ratio": 1 - share,
                                       "delay_ms_mean": delay.get(lp) if share > 0 else None}
        return out


def _shortest(fibres: Dict[str, Dict[str, Any]], up: Dict[str, bool], src: str, dst: str) -> Optional[List[str]]:
    adj: Dict[str, List[Tuple[str, str, float]]] = {}
    for fid, f in fibres.items():
        if up.get(fid):
            adj.setdefault(f["a"], []).append((f["z"], fid, f["length_km"]))
            adj.setdefault(f["z"], []).append((f["a"], fid, f["length_km"]))
    best = {src: 0.0}
    heap = [(0.0, src, [])]
    while heap:
        d, n, path = heapq.heappop(heap)
        if n == dst:
            return path
        if d > best.get(n, float("inf")):
            continue
        for m, fid, km in adj.get(n, []):
            nd = d + km
            if nd < best.get(m, float("inf")):
                best[m] = nd
                heapq.heappush(heap, (nd, m, path + [fid]))
    return None


EXAMPLE = {
    "schema_version": "1.0", "domain": "optical", "snapshot_id": "<the snapshot it came from>",
    "question": "if fibre F-ab is cut, does LP-1 keep a 3 dB margin after it restores?",
    "topology": {"nodes": [{"id": "A"}, {"id": "B"}, {"id": "C"}],
                 "links": [{"id": "F-ab", "a": "A", "z": "B", "length_km": 240},
                           {"id": "F-ac", "a": "A", "z": "C", "length_km": 400},
                           {"id": "F-cb", "a": "C", "z": "B", "length_km": 380}]},
    "traffic": {"lightpaths": [{"id": "LP-1", "src": "A", "dst": "B", "rate_gbps": 200, "path": ["F-ab"], "restore": True}],
                "demands": [{"id": "acme", "lightpath": "LP-1", "rate_mbps": 40000}]},
    "change": {"events": [{"t": 0, "type": "link_down", "target": "F-ab"}]},
    "requirements": [{"demand": "LP-1", "kpi": "margin_db", "op": ">=", "value": 3}],
    "uncertain_params": [{"path": "links.F-ac.loss_db_per_km", "dist": "normal", "mean": 0.21, "std": 0.01},
                         {"path": "links.F-cb.loss_db_per_km", "dist": "normal", "mean": 0.21, "std": 0.01}],
    "seed_policy": {"runs": 10, "max_runs": 40},
}
