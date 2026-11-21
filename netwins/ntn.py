"""
The NTN twin: non-terrestrial networks. Satellites (LEO, MEO, GEO) and high-altitude platforms (HAPS) carrying
traffic between ground sites (user terminals, gateways), over a window of time in which the satellites move.

  scenario (domain "ntn")
    topology   sites (terminals and gateways: where they are, their antenna's EIRP and G/T, the lowest
               elevation they work at, the rain on them), satellites (an orbit, or a HAPS's fixed position;
               EIRP, G/T, capacity, inter-satellite links), and constellations (Walker delta: planes x
               satellites per plane at one altitude and inclination, expanded to satellites <id>-P<p>-S<s>)
    radio      the bands and the link allowances: uplink and downlink frequency, bandwidth, roll-off,
               gaseous and pointing loss, how long a handover interrupts, the ground and processing delay
    traffic    flows (src site -> dst site, rate)
    events     satellite_down / satellite_up, site_down / site_up, isl_down / isl_up, rain (on a site, mm/h)
    change     set values, events, add or remove flows

  engine (NTNEngine, per member), every step_s over duration_s
    1. where each satellite is (circular orbits, the Earth turning under them; a HAPS stays put)
    2. which satellites each site sees above its minimum elevation; a flow's route: up from its source to a
       satellite it sees, through inter-satellite links when the satellites have them (a +Grid between
       neighbours in a constellation), down to its destination; the serving satellite is kept while it can
       be (a handover, when it cannot, interrupts the flow for handover_ms)
    3. each hop's link budget: free-space loss, gaseous loss and rain fade (ITU-R P.838 coefficients, the
       P.618 path reduction, simplified), pointing loss, the receiver's G/T: Es/N0, and from it the DVB-S2X
       MODCOD (adaptive coding and modulation) and the hop's capacity; flows on one satellite share it
    4. per flow: availability (the share of the window it is carried), throughput, loss, delay (the slant
       ranges at the speed of light, the processing on each satellite, the ground segment), the lowest link
       margin, the handovers, the largest Doppler shift on its access link

  per flow (the `demand` of a requirement names it):
    availability, throughput_mbps, loss_ratio, delay_ms_mean, delay_ms_max, margin_db_min, handovers,
    doppler_khz_max
"""
from __future__ import annotations

import copy
import heapq
import math
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import Field, field_validator

from .schema import SCHEMA_VERSIONS, SeedPolicy, SetValue, Uncertain, _Strict

RE = 6371.0                  # km, the Earth's mean radius
MU = 398600.4418             # km^3/s^2
WE = 7.2921159e-5            # rad/s, the Earth's rotation
C_KM_S = 299792.458
LIMITS = {"sites": 200, "satellites": 3000, "flows": 200, "steps": 1000, "altitude_km": (15.0, 40000.0),
          "rate_mbps": (0.001, 100_000.0), "rain_mm_h": (0.0, 200.0), "frequency_ghz": (1.0, 50.0)}
KPIS = ("availability", "throughput_mbps", "loss_ratio", "delay_ms_mean", "delay_ms_max", "margin_db_min",
        "handovers", "doppler_khz_max")
NOT_MODELLED = ("Circular orbits on a spherical Earth (no J2, drag or eccentricity); a simplified ITU-R rain and "
                "gaseous loss, no scintillation or cloud; no interference between beams or satellites; the "
                "receiver copes with Doppler (its shift is reported, not its effect); handovers as a fixed "
                "interruption; a fluid model of traffic (no TCP, no queues).")
FIDELITY = ("geometry and link budget per step: orbital positions, visibility and slant range, free-space, "
            "gaseous and rain loss, DVB-S2X adaptive coding and modulation; traffic: fluid, shared per satellite")

# DVB-S2/S2X MODCODs: the Es/N0 (dB) each needs, and its spectral efficiency (bit per symbol)
MODCODS = [(-2.35, 0.49, "QPSK 1/4"), (-1.24, 0.66, "QPSK 1/3"), (1.00, 0.99, "QPSK 1/2"), (3.10, 1.32, "QPSK 2/3"),
           (4.03, 1.49, "QPSK 3/4"), (5.18, 1.65, "QPSK 5/6"), (6.62, 1.98, "8PSK 2/3"), (7.91, 2.23, "8PSK 3/4"),
           (9.35, 2.48, "8PSK 5/6"), (10.21, 2.97, "16APSK 3/4"), (11.03, 3.30, "16APSK 5/6"),
           (13.64, 3.95, "32APSK 4/5"), (14.28, 4.12, "32APSK 5/6"), (17.73, 4.94, "64APSK 5/6")]
# ITU-R P.838-3 rain coefficients (k, alpha), interpolated in log frequency between these
RAIN_K = [(1.0, 0.0000387, 0.912), (2.0, 0.000154, 0.963), (4.0, 0.000650, 1.121), (8.0, 0.00454, 1.327),
          (12.0, 0.0239, 1.1825), (14.0, 0.0367, 1.154), (20.0, 0.0751, 1.099), (30.0, 0.2403, 0.9485),
          (40.0, 0.4431, 0.8673), (50.0, 0.6600, 0.8084)]


class Site(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    kind: Literal["terminal", "gateway"] = "terminal"
    lat_deg: float = Field(ge=-90, le=90)
    lon_deg: float = Field(ge=-180, le=180)
    alt_m: float = 0.0
    eirp_dbw: float = 45.0               # what it sends with
    g_t_dbk: float = 13.0                # what it receives with
    min_elevation_deg: float = 25.0
    rain_mm_h: float = 0.0               # the rain on it now (0: clear sky)


class Satellite(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    kind: Literal["leo", "meo", "geo", "haps"] = "leo"
    altitude_km: float = 550.0
    inclination_deg: float = 53.0
    raan_deg: float = 0.0                # right ascension of the ascending node
    phase_deg: float = 0.0               # where it is on its orbit at t = 0 (a GEO: its longitude)
    lat_deg: float = 0.0                 # a HAPS: where it stays
    lon_deg: float = 0.0
    eirp_dbw: float = 40.0
    g_t_dbk: float = 10.0
    capacity_mbps: float = 4000.0        # what one satellite carries at most (all its beams)
    isl: bool = False
    isl_capacity_mbps: float = 20000.0


class Constellation(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,32}$")
    planes: int = Field(ge=1, le=100)
    per_plane: int = Field(ge=1, le=100)
    altitude_km: float = 550.0
    inclination_deg: float = 53.0
    phasing: int = 1                     # Walker F: the phase shift between planes, in slots
    eirp_dbw: float = 40.0
    g_t_dbk: float = 10.0
    capacity_mbps: float = 4000.0
    isl: bool = True
    isl_capacity_mbps: float = 20000.0


class NTopology(_Strict):
    sites: List[Site]
    satellites: List[Satellite] = Field(default_factory=list)
    constellations: List[Constellation] = Field(default_factory=list)


class Radio(_Strict):
    uplink_ghz: float = 30.0
    downlink_ghz: float = 20.0
    bandwidth_mhz: float = 250.0
    rolloff: float = 0.2
    gaseous_loss_db: float = 0.4         # at zenith; more at low elevation
    pointing_loss_db: float = 0.3
    handover_ms: float = 50.0
    processing_ms: float = 1.0           # on each satellite
    ground_delay_ms: float = 5.0         # gateway to the core


class Flow(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    src: str
    dst: str
    rate_mbps: float = Field(gt=0)


class NTraffic(_Strict):
    flows: List[Flow] = Field(default_factory=list)


class NEvent(_Strict):
    t: float = 0.0
    type: Literal["satellite_down", "satellite_up", "site_down", "site_up", "isl_down", "isl_up", "rain"]
    target: str                          # a satellite, a site, or "all" for isl_down / isl_up
    value: Optional[float] = None        # rain: mm/h


class NRequirement(_Strict):
    demand: str
    kpi: Literal["availability", "throughput_mbps", "loss_ratio", "delay_ms_mean", "delay_ms_max", "margin_db_min",
                 "handovers", "doppler_khz_max"]
    op: Literal["<", "<=", ">", ">="]
    value: float


class NChange(_Strict):
    set: List[SetValue] = Field(default_factory=list)
    events: List[NEvent] = Field(default_factory=list)
    add_flows: List[Flow] = Field(default_factory=list)
    remove_flows: List[str] = Field(default_factory=list)


class NScenario(_Strict):
    schema_version: str = "1.0"
    domain: Literal["ntn"]
    snapshot_id: str = ""
    question: str = ""
    topology: NTopology
    radio: Radio = Field(default_factory=Radio)
    traffic: NTraffic = Field(default_factory=NTraffic)
    events: List[NEvent] = Field(default_factory=list)
    change: Optional[NChange] = None
    duration_s: float = Field(default=600.0, gt=0, le=86400)
    step_s: float = Field(default=10.0, gt=0, le=3600)
    requirements: List[NRequirement] = Field(default_factory=list)
    uncertain_params: List[Uncertain] = Field(default_factory=list)
    seed_policy: SeedPolicy = Field(default_factory=lambda: SeedPolicy(runs=10, max_runs=40, batch=10))

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: str) -> str:
        if v not in SCHEMA_VERSIONS:
            raise ValueError(f"unknown schema_version {v!r}; this twin reads {SCHEMA_VERSIONS}")
        return v


SETTABLE = {"sites": {"eirp_dbw", "g_t_dbk", "min_elevation_deg", "rain_mm_h"},
            "satellites": {"eirp_dbw", "g_t_dbk", "capacity_mbps", "isl_capacity_mbps"},
            "constellations": {"eirp_dbw", "g_t_dbk", "capacity_mbps", "isl_capacity_mbps"},
            "flows": {"rate_mbps"},
            "radio": {"uplink_ghz", "downlink_ghz", "bandwidth_mhz", "gaseous_loss_db", "pointing_loss_db",
                      "handover_ms", "processing_ms", "ground_delay_ms"}}
WHERE = {"sites": "topology", "satellites": "topology", "constellations": "topology", "flows": "traffic", "radio": ""}


def expand(topology: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every satellite: the ones listed, and each constellation's (Walker delta), as <id>-P<plane>-S<slot>."""
    sats = [dict(s) for s in topology.get("satellites") or []]
    for c in topology.get("constellations") or []:
        P, S, F = c["planes"], c["per_plane"], c.get("phasing", 1)
        for p in range(P):
            for k in range(S):
                sats.append({"id": f"{c['id']}-P{p}-S{k}", "kind": "leo" if c["altitude_km"] < 2000 else "meo",
                             "altitude_km": c["altitude_km"], "inclination_deg": c["inclination_deg"],
                             "raan_deg": 360.0 * p / P, "phase_deg": 360.0 * k / S + 360.0 * F * p / (P * S),
                             "eirp_dbw": c["eirp_dbw"], "g_t_dbk": c["g_t_dbk"], "capacity_mbps": c["capacity_mbps"],
                             "isl": c.get("isl", True), "isl_capacity_mbps": c.get("isl_capacity_mbps", 20000.0),
                             "_plane": (c["id"], p, k, P, S)})
    return sats


def problems(sc: NScenario) -> List[str]:
    out: List[str] = []
    sites = {s.id for s in sc.topology.sites}
    sats = expand(sc.model_dump()["topology"])
    sat_ids = {s["id"] for s in sats}
    for key, items in (("sites", sc.topology.sites), ("flows", sc.traffic.flows)):
        if len(items) > LIMITS[key]:
            out.append(f"{len(items)} {key}: the cap is {LIMITS[key]}")
        ids = [x.id for x in items]
        if len(ids) != len(set(ids)):
            out.append(f"{key}: ids repeat")
    if len(sats) > LIMITS["satellites"]:
        out.append(f"{len(sats)} satellites: the cap is {LIMITS['satellites']}")
    if len(sat_ids) != len(sats):
        out.append("satellites: ids repeat (a listed satellite has a constellation's name)")
    if sites & sat_ids:
        out.append(f"a site and a satellite share a name: {sorted(sites & sat_ids)[:3]}")
    if sc.duration_s / sc.step_s > LIMITS["steps"]:
        out.append(f"duration_s / step_s = {sc.duration_s / sc.step_s:.0f} steps: the cap is {LIMITS['steps']}")
    lo, hi = LIMITS["altitude_km"]
    for s in sats:
        if s["kind"] == "haps" and not 15 <= s["altitude_km"] <= 50:
            out.append(f"satellite {s['id']}: a HAPS flies at 15-50 km, not {s['altitude_km']}")
        elif not lo <= s["altitude_km"] <= hi:
            out.append(f"satellite {s['id']}: altitude_km {s['altitude_km']} is outside {lo}..{hi}")
    flo, fhi = LIMITS["frequency_ghz"]
    for f in ("uplink_ghz", "downlink_ghz"):
        v = getattr(sc.radio, f)
        if not flo <= v <= fhi:
            out.append(f"radio.{f} {v} is outside {flo}..{fhi} GHz")
    flows = {f.id for f in sc.traffic.flows} | ({f.id for f in sc.change.add_flows} if sc.change else set())
    for f in list(sc.traffic.flows) + (list(sc.change.add_flows) if sc.change else []):
        for end in (f.src, f.dst):
            if end not in sites:
                out.append(f"flow {f.id}: no site {end!r}")
        if f.src == f.dst:
            out.append(f"flow {f.id}: src and dst are the same site")
    for e in list(sc.events) + (list(sc.change.events) if sc.change else []):
        if e.type.startswith("satellite") and e.target not in sat_ids:
            out.append(f"event {e.type}: no satellite {e.target!r}")
        if (e.type.startswith("site") or e.type == "rain") and e.target not in sites:
            out.append(f"event {e.type}: no site {e.target!r}")
        if e.type.startswith("isl") and e.target != "all" and e.target not in sat_ids:
            out.append(f"event {e.type}: target a satellite or 'all', not {e.target!r}")
        if e.type == "rain" and (e.value is None or not 0 <= e.value <= LIMITS["rain_mm_h"][1]):
            out.append(f"event rain on {e.target}: value is the rain in mm/h, 0..{LIMITS['rain_mm_h'][1]}")
        if e.t > sc.duration_s:
            out.append(f"event {e.type} on {e.target} at t={e.t} s is after the window ({sc.duration_s} s)")
    if sc.change:
        for fid in sc.change.remove_flows:
            if fid not in flows:
                out.append(f"change: no flow {fid!r} to remove")
    for r in sc.requirements:
        if r.demand not in flows:
            out.append(f"requirement on {r.demand!r}: not a flow")
    return out


def resolve(data: Dict[str, Any], path: str):
    from .paths import resolve as _r
    return _r(data, path, SETTABLE, WHERE)


def set_path(data: Dict[str, Any], path: str, value: Any) -> None:
    obj, field = resolve(data, path)
    obj[field] = value


def clamp(path: str, v: float) -> float:
    field = path.rsplit(".", 1)[-1]
    rng = LIMITS.get(field) if isinstance(LIMITS.get(field), tuple) else None
    if field in ("uplink_ghz", "downlink_ghz"):
        rng = LIMITS["frequency_ghz"]
    if field in ("rain_mm_h", "rate_mbps", "capacity_mbps", "isl_capacity_mbps", "handover_ms", "processing_ms",
                 "ground_delay_ms", "gaseous_loss_db", "pointing_loss_db", "bandwidth_mhz"):
        rng = rng or (0.0, float("inf"))
    return min(max(v, rng[0]), rng[1]) if rng else v


def baseline_and_changed(sc: NScenario) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    base = sc.model_dump(exclude={"change"})
    changed = copy.deepcopy(base)
    if sc.change:
        for s in sc.change.set:
            set_path(changed, s.path, s.value)
        changed["events"] = [*changed["events"], *[e.model_dump() for e in sc.change.events]]
        gone = set(sc.change.remove_flows)
        changed["traffic"]["flows"] = [f for f in changed["traffic"]["flows"] if f["id"] not in gone]
        changed["traffic"]["flows"] += [f.model_dump() for f in sc.change.add_flows]
    return base, changed


def new_entities(sc: NScenario) -> set:
    return {f.id for f in sc.change.add_flows} if sc.change else set()


# ----------------------------------------------------------------------------- the physics

def _site_xyz(s: Dict[str, Any]) -> Tuple[float, float, float]:
    r = RE + s.get("alt_m", 0.0) / 1000.0
    la, lo = math.radians(s["lat_deg"]), math.radians(s["lon_deg"])
    return (r * math.cos(la) * math.cos(lo), r * math.cos(la) * math.sin(lo), r * math.sin(la))


def _sat_xyz(s: Dict[str, Any], t: float) -> Tuple[float, float, float]:
    """Where a satellite is at t (s), in Earth-fixed coordinates."""
    if s["kind"] == "haps":
        return _site_xyz({"lat_deg": s["lat_deg"], "lon_deg": s["lon_deg"], "alt_m": s["altitude_km"] * 1000.0})
    r = RE + s["altitude_km"]
    if s["kind"] == "geo":
        lo = math.radians(s["phase_deg"])
        return (r * math.cos(lo), r * math.sin(lo), 0.0)
    n = math.sqrt(MU / r ** 3)
    u = math.radians(s["phase_deg"]) + n * t
    i, om = math.radians(s["inclination_deg"]), math.radians(s["raan_deg"])
    x = r * (math.cos(om) * math.cos(u) - math.sin(om) * math.sin(u) * math.cos(i))
    y = r * (math.sin(om) * math.cos(u) + math.cos(om) * math.sin(u) * math.cos(i))
    z = r * math.sin(u) * math.sin(i)
    th = WE * t                                     # the Earth has turned under the orbit
    return (x * math.cos(th) + y * math.sin(th), -x * math.sin(th) + y * math.cos(th), z)


def _look(site: Tuple[float, float, float], sat: Tuple[float, float, float]) -> Tuple[float, float]:
    """(elevation in degrees, slant range in km) from a site to a satellite."""
    d = (sat[0] - site[0], sat[1] - site[1], sat[2] - site[2])
    rng = math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2)
    up = math.sqrt(site[0] ** 2 + site[1] ** 2 + site[2] ** 2)
    sin_el = (d[0] * site[0] + d[1] * site[1] + d[2] * site[2]) / (rng * up)
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_el)))), rng


def _rain_coeffs(f_ghz: float) -> Tuple[float, float]:
    pts = RAIN_K
    if f_ghz <= pts[0][0]:
        return pts[0][1], pts[0][2]
    for (f1, k1, a1), (f2, k2, a2) in zip(pts, pts[1:]):
        if f_ghz <= f2:
            w = (math.log(f_ghz) - math.log(f1)) / (math.log(f2) - math.log(f1))
            return math.exp(math.log(k1) + w * (math.log(k2) - math.log(k1))), a1 + w * (a2 - a1)
    return pts[-1][1], pts[-1][2]


def rain_db(f_ghz: float, rain_mm_h: float, el_deg: float, site_alt_km: float = 0.0, rain_height_km: float = 3.0) -> float:
    """Rain fade on a slant path (ITU-R P.838 specific attenuation, P.618 path reduction, simplified)."""
    if rain_mm_h <= 0:
        return 0.0
    el = math.radians(max(el_deg, 5.0))
    k, a = _rain_coeffs(f_ghz)
    gamma = k * rain_mm_h ** a                                   # dB/km
    ls = max(rain_height_km - site_alt_km, 0.0) / math.sin(el)   # slant path through the rain
    l0 = 35.0 * math.exp(-0.015 * min(rain_mm_h, 100.0))
    r = 1.0 / (1.0 + ls * math.cos(el) / l0)
    return gamma * ls * r


def hop(eirp_dbw: float, g_t_dbk: float, rng_km: float, f_ghz: float, el_deg: float, rain_mm_h: float,
        radio: Dict[str, Any]) -> Dict[str, float]:
    """One link: Es/N0 (dB), the MODCOD it allows, its capacity (Mbit/s) and margin over the lowest MODCOD."""
    fspl = 20 * math.log10(rng_km) + 20 * math.log10(f_ghz) + 92.45
    gas = radio["gaseous_loss_db"] / math.sin(math.radians(max(el_deg, 5.0)))
    loss = fspl + gas + rain_db(f_ghz, rain_mm_h, el_deg) + radio["pointing_loss_db"]
    cn0 = eirp_dbw - loss + g_t_dbk + 228.6                       # dBHz
    rs = radio["bandwidth_mhz"] * 1e6 / (1 + radio["rolloff"])   # symbols/s
    esn0 = cn0 - 10 * math.log10(rs)
    best = None
    for need, eff, name in MODCODS:
        if esn0 >= need:
            best = (eff, name)
    cap = 0.0 if best is None else rs * best[0] / 1e6
    return {"esn0_db": esn0, "capacity_mbps": cap, "modcod": best[1] if best else None,
            "margin_db": esn0 - MODCODS[0][0], "loss_db": loss}


class NTNEngine:
    """One member: the window, step by step."""

    @staticmethod
    def version() -> str:
        return "netwins-ntn 0.1 (geometry + link budget, DVB-S2X ACM)"

    def run(self, member: Dict[str, Any], seed: int) -> Dict[str, Any]:
        radio = member["radio"]
        sites = {s["id"]: dict(s) for s in member["topology"]["sites"]}
        sats = {s["id"]: s for s in expand(member["topology"])}
        site_xyz = {k: _site_xyz(s) for k, s in sites.items()}
        flows = member["traffic"]["flows"]
        events = sorted(member.get("events", []), key=lambda e: e.get("t", 0))
        sat_up = {k: True for k in sats}
        site_up = {k: True for k in sites}
        isl_up = {k: True for k in sats}
        neigh = self._grid(sats)
        dt = member.get("step_s", 10.0)
        steps = max(1, int(round(member.get("duration_s", 600.0) / dt)))
        acc = {f["id"]: {"up": 0.0, "thr": 0.0, "delays": [], "margin": None, "ho": 0, "dop": 0.0} for f in flows}
        serving: Dict[str, Optional[str]] = {f["id"]: None for f in flows}
        ei = 0
        for step in range(steps):
            t = step * dt
            while ei < len(events) and events[ei].get("t", 0) <= t:
                self._apply(events[ei], sat_up, site_up, isl_up, sites)
                ei += 1
            pos = {k: _sat_xyz(s, t) for k, s in sats.items() if sat_up[k]}
            pos_next = {k: _sat_xyz(s, t + 1.0) for k, s in sats.items() if sat_up[k] and s["kind"] in ("leo", "meo")}
            seen = {sid: {k: _look(site_xyz[sid], p) for k, p in pos.items()} for sid in sites}
            routes, load = {}, {}
            for f in flows:
                r = self._route(f, sites, sats, pos, seen, site_up, isl_up, neigh, radio, serving[f["id"]])
                if r and serving[f["id"]] is not None and r["serving"] != serving[f["id"]]:
                    r["handover"] = True
                if r:
                    serving[f["id"]] = r["serving"]
                    for k in r["sats"]:
                        load[k] = load.get(k, 0.0) + f["rate_mbps"]
                routes[f["id"]] = r
            for f in flows:
                a, r = acc[f["id"]], routes[f["id"]]
                if not r:
                    continue
                share = min(1.0, r["capacity_mbps"] / f["rate_mbps"])
                for k in r["sats"]:
                    share = min(share, sats[k]["capacity_mbps"] / max(load[k], 1e-9))
                cut = min(1.0, radio["handover_ms"] / 1000.0 / dt) if r.get("handover") else 0.0
                if r.get("handover"):
                    a["ho"] += 1
                carried = share * (1 - cut)
                if carried > 0:
                    a["up"] += 1 - cut
                    a["thr"] += f["rate_mbps"] * carried
                    a["delays"].append(r["delay_ms"])
                a["margin"] = r["margin_db"] if a["margin"] is None else min(a["margin"], r["margin_db"])
                k0 = r["serving"]
                if k0 in pos_next:
                    _, now = seen[f["src"]][k0]
                    _, nxt = _look(site_xyz[f["src"]], pos_next[k0])
                    a["dop"] = max(a["dop"], abs(nxt - now) / C_KM_S * radio["uplink_ghz"] * 1e6)   # kHz
        out: Dict[str, Any] = {"run": seed, "demands": {}}
        for f in flows:
            a = acc[f["id"]]
            thr = a["thr"] / steps
            d = a["delays"]
            out["demands"][f["id"]] = {
                "availability": round(a["up"] / steps, 4), "throughput_mbps": thr,
                "loss_ratio": max(0.0, 1 - thr / f["rate_mbps"]),
                "delay_ms_mean": (sum(d) / len(d)) if d else None, "delay_ms_max": max(d) if d else None,
                "margin_db_min": round(a["margin"], 3) if a["margin"] is not None else None,
                "handovers": a["ho"], "doppler_khz_max": round(a["dop"], 2)}
        return out

    @staticmethod
    def _apply(e: Dict[str, Any], sat_up, site_up, isl_up, sites) -> None:
        kind, tgt = e["type"], e["target"]
        if kind in ("satellite_down", "satellite_up") and tgt in sat_up:
            sat_up[tgt] = kind == "satellite_up"
        elif kind in ("site_down", "site_up") and tgt in site_up:
            site_up[tgt] = kind == "site_up"
        elif kind in ("isl_down", "isl_up"):
            for k in (isl_up if tgt == "all" else [tgt]):
                if k in isl_up:
                    isl_up[k] = kind == "isl_up"
        elif kind == "rain" and tgt in sites:
            sites[tgt]["rain_mm_h"] = float(e.get("value") or 0.0)

    @staticmethod
    def _grid(sats: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
        """Inter-satellite links: +Grid in a constellation (the two neighbours in the plane, the same slot in
        the two neighbouring planes); a listed satellite with ISLs links to any other one within 5000 km."""
        out: Dict[str, List[str]] = {k: [] for k in sats}
        by_slot = {s["_plane"][:3]: k for k, s in sats.items() if "_plane" in s}
        for k, s in sats.items():
            if not s.get("isl") or "_plane" not in s:
                continue
            cid, p, i, P, S = s["_plane"]
            for key in ((cid, p, (i + 1) % S), (cid, p, (i - 1) % S), (cid, (p + 1) % P, i), (cid, (p - 1) % P, i)):
                o = by_slot.get(key)
                if o and o != k and sats[o].get("isl"):
                    out[k].append(o)
        loose = [k for k, s in sats.items() if s.get("isl") and "_plane" not in s]
        for k in loose:
            out[k] += [o for o in sats if o != k and sats[o].get("isl")]
        return out

    def _route(self, f, sites, sats, pos, seen, site_up, isl_up, neigh, radio, keep) -> Optional[Dict[str, Any]]:
        src, dst = sites[f["src"]], sites[f["dst"]]
        if not (site_up[f["src"]] and site_up[f["dst"]]):
            return None
        vis_src = {k: v for k, v in seen[f["src"]].items() if v[0] >= src["min_elevation_deg"]}
        vis_dst = {k: v for k, v in seen[f["dst"]].items() if v[0] >= dst["min_elevation_deg"]}
        if not vis_src or not vis_dst:
            return None
        # Shortest path in km: up to a satellite the source sees, across ISLs, down to the destination. The
        # satellite serving the source is kept while it still works (fewer handovers), else the nearest.
        starts = {k: v[1] for k, v in vis_src.items()}
        if keep in starts:
            starts = {keep: starts[keep]}
        best: Dict[str, float] = dict(starts)
        prev: Dict[str, Optional[str]] = {k: None for k in starts}
        heap = [(d, k) for k, d in starts.items()]
        heapq.heapify(heap)
        end, end_cost = None, float("inf")
        while heap:
            d, k = heapq.heappop(heap)
            if d > best.get(k, float("inf")) or d >= end_cost:
                continue
            if k in vis_dst and d + vis_dst[k][1] < end_cost:
                end, end_cost = k, d + vis_dst[k][1]
            if not isl_up.get(k):
                continue
            for o in neigh.get(k, []):
                if o not in pos or not isl_up.get(o):
                    continue
                a, b = pos[k], pos[o]
                hop_km = math.dist(a, b)
                if hop_km > 6000 or not self._clear(a, b):
                    continue
                nd = d + hop_km
                if nd < best.get(o, float("inf")):
                    best[o], prev[o] = nd, k
                    heapq.heappush(heap, (nd, o))
        if end is None:
            if keep is not None:                         # the serving satellite no longer reaches: hand over
                return self._route(f, sites, sats, pos, seen, site_up, isl_up, neigh, radio, None)
            return None
        chain, k = [], end
        while k is not None:
            chain.append(k)
            k = prev[k]
        chain.reverse()
        first, last = chain[0], chain[-1]
        el_u, rng_u = vis_src[first]
        el_d, rng_d = vis_dst[last]
        up = hop(src["eirp_dbw"], sats[first]["g_t_dbk"], rng_u, radio["uplink_ghz"], el_u, src["rain_mm_h"], radio)
        down = hop(sats[last]["eirp_dbw"], dst["g_t_dbk"], rng_d, radio["downlink_ghz"], el_d, dst["rain_mm_h"], radio)
        cap = min(up["capacity_mbps"], down["capacity_mbps"],
                  *[min(sats[a]["isl_capacity_mbps"], sats[b]["isl_capacity_mbps"]) for a, b in zip(chain, chain[1:])])
        isl_km = end_cost - rng_u - rng_d
        delay = ((rng_u + rng_d + isl_km) / C_KM_S * 1000.0 + radio["processing_ms"] * len(chain) + radio["ground_delay_ms"])
        return {"serving": first, "sats": chain, "capacity_mbps": cap, "delay_ms": delay,
                "margin_db": min(up["margin_db"], down["margin_db"]), "modcod": (up["modcod"], down["modcod"])}

    @staticmethod
    def _clear(a, b) -> bool:
        """Whether the line between two satellites clears the Earth and 80 km of atmosphere."""
        ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        L2 = ab[0] ** 2 + ab[1] ** 2 + ab[2] ** 2
        t = max(0.0, min(1.0, -(a[0] * ab[0] + a[1] * ab[1] + a[2] * ab[2]) / L2)) if L2 else 0.0
        c = (a[0] + t * ab[0], a[1] + t * ab[1], a[2] + t * ab[2])
        return math.sqrt(c[0] ** 2 + c[1] ** 2 + c[2] ** 2) > RE + 80.0


class FakeNTNEngine(NTNEngine):
    """The same engine (it is fast and local): tests use the real thing."""


EXAMPLE = {
    "schema_version": "1.0", "domain": "ntn", "snapshot_id": "",
    "question": "if heavy rain (25 mm/h) falls on the Turin terminal, does it keep 99% availability and 50 ms?",
    "topology": {
        "sites": [{"id": "T-turin", "kind": "terminal", "lat_deg": 45.07, "lon_deg": 7.69, "eirp_dbw": 45, "g_t_dbk": 13},
                  {"id": "GW-fucino", "kind": "gateway", "lat_deg": 42.0, "lon_deg": 13.6, "eirp_dbw": 70, "g_t_dbk": 30,
                   "min_elevation_deg": 20}],
        "constellations": [{"id": "LEO", "planes": 24, "per_plane": 22, "altitude_km": 550, "inclination_deg": 53,
                            "phasing": 1, "eirp_dbw": 40, "g_t_dbk": 10, "capacity_mbps": 4000, "isl": True}]},
    "radio": {"uplink_ghz": 30, "downlink_ghz": 20, "bandwidth_mhz": 250},
    "traffic": {"flows": [{"id": "backhaul-1", "src": "T-turin", "dst": "GW-fucino", "rate_mbps": 100}]},
    "change": {"events": [{"t": 0, "type": "rain", "target": "T-turin", "value": 25}]},
    "duration_s": 600, "step_s": 10,
    "requirements": [{"demand": "backhaul-1", "kpi": "availability", "op": ">=", "value": 0.99},
                     {"demand": "backhaul-1", "kpi": "delay_ms_mean", "op": "<", "value": 50}],
    "uncertain_params": [{"path": "sites.T-turin.g_t_dbk", "dist": "normal", "mean": 13, "std": 0.5}],
    "seed_policy": {"runs": 10, "max_runs": 20},
}
