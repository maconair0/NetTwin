"""
The scenario: what the agent writes, in JSON, and the only thing that reaches the engine.

Pydantic is the single source of truth (`json_schema()` exports it for the agent and for
constrained decoding). Validation has two stages:

  schema      types, required fields, the schema version (an unknown version is rejected)
  semantics   `problems(scenario)`: references exist, values are in physical and validated ranges,
              events fall inside the duration, the scenario is under the size cap, every uncertain
              parameter and every change points at a real field

`change` is the what-if. The baseline is the same scenario without it (paired runs: same seeds,
same sampled inputs).
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSIONS = ("1.0",)

# Validated ranges of the IP engine (v0). Outside them the twin refuses rather than extrapolates.
LIMITS = {
    "nodes": 200, "links": 400, "demands": 200, "events": 100, "duration_s": (1.0, 600.0),
    "rate_mbps": (0.01, 100_000.0), "delay_ms": (0.0, 1000.0), "queue_pkts": (1, 100_000),
    "loss_rate": (0.0, 0.5), "demand_rate_mbps": (0.001, 100_000.0), "packet_bytes": (64, 9000),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Node(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    role: str = ""


class Link(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    a: str
    z: str
    rate_mbps: float = 100.0
    delay_ms: float = 1.0
    queue_pkts: int = 100
    loss_rate: float = 0.0


class Demand(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    src: str
    dst: str
    protocol: Literal["udp", "tcp"] = "udp"
    rate_mbps: float = 1.0
    packet_bytes: int = 1200
    start_s: float = 1.0
    stop_s: Optional[float] = None


class Event(_Strict):
    t: float
    type: Literal["link_down", "link_up"]
    target: str


class Topology(_Strict):
    nodes: List[Node]
    links: List[Link]


class Traffic(_Strict):
    demands: List[Demand] = Field(default_factory=list)


class Requirement(_Strict):
    demand: str
    kpi: Literal["throughput_mbps", "delay_ms_mean", "jitter_ms_mean", "loss_ratio"]
    op: Literal["<", "<=", ">", ">="]
    value: float


class SetValue(_Strict):
    path: str                       # "links.L2.rate_mbps", "demands.D1.rate_mbps"
    value: Union[float, int, str]


class Change(_Strict):
    """What is different on the changed side: values set, events injected, demands added (a new
    service) or removed (a service taken down)."""
    set: List[SetValue] = Field(default_factory=list)
    events: List[Event] = Field(default_factory=list)
    add_demands: List["Demand"] = Field(default_factory=list)
    remove_demands: List[str] = Field(default_factory=list)


class Uncertain(_Strict):
    path: str
    dist: Literal["normal", "uniform", "lognormal"]
    mean: Optional[float] = None    # normal, lognormal (of the value itself)
    std: Optional[float] = None
    low: Optional[float] = None     # uniform
    high: Optional[float] = None


class SeedPolicy(_Strict):
    runs: int = Field(default=20, ge=2, le=500)
    max_runs: int = Field(default=60, ge=2, le=1000)
    batch: int = Field(default=10, ge=1, le=100)
    adaptive: bool = True
    base_seed: int = Field(default=1, ge=1)


class Scenario(_Strict):
    schema_version: str = "1.0"
    domain: Literal["ip"] = "ip"
    snapshot_id: str = ""
    question: str = ""
    topology: Topology
    traffic: Traffic = Field(default_factory=Traffic)
    events: List[Event] = Field(default_factory=list)
    change: Optional[Change] = None
    duration_s: float = 60.0
    requirements: List[Requirement] = Field(default_factory=list)
    uncertain_params: List[Uncertain] = Field(default_factory=list)
    seed_policy: SeedPolicy = Field(default_factory=SeedPolicy)

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: str) -> str:
        if v not in SCHEMA_VERSIONS:
            raise ValueError(f"unknown schema_version {v!r}; this twin reads {SCHEMA_VERSIONS}")
        return v


def json_schema() -> Dict[str, Any]:
    return Scenario.model_json_schema()


# ----- paths: "links.L2.rate_mbps" -> that link's field -----

_PATH = re.compile(r"^(links|demands|nodes)\.([A-Za-z0-9_.:-]+?)\.([a-z_]+)$")
_SETTABLE = {"links": {"rate_mbps", "delay_ms", "queue_pkts", "loss_rate"},
             "demands": {"rate_mbps", "packet_bytes", "start_s", "stop_s"}}


def _resolve(data: Dict[str, Any], path: str):
    """(the object dict, field) a path points at in a scenario dict; ValueError if it points nowhere."""
    m = _PATH.match(path or "")
    if not m:
        raise ValueError(f"{path!r} is not a path like links.L1.delay_ms")
    kind, ident, field = m.groups()
    if field not in _SETTABLE.get(kind, set()):
        raise ValueError(f"{path!r}: {kind} field {field!r} cannot be set or sampled "
                         f"(allowed: {sorted(_SETTABLE.get(kind, set()))})")
    items = data["topology"][kind] if kind in ("links", "nodes") else data["traffic"][kind]
    for obj in items:
        if obj.get("id") == ident:
            return obj, field
    raise ValueError(f"{path!r}: no {kind[:-1]} {ident!r}")


def set_path(data: Dict[str, Any], path: str, value: Any) -> None:
    obj, field = _resolve(data, path)
    obj[field] = value


# ----- semantics -----

def problems(sc: Scenario) -> List[str]:
    """Everything wrong with a scenario that the schema cannot see. Empty means it may run."""
    out: List[str] = []
    nodes = [n.id for n in sc.topology.nodes]
    links = [l.id for l in sc.topology.links]
    demands = [d.id for d in sc.traffic.demands]
    for kind, ids in (("node", nodes), ("link", links), ("demand", demands)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            out.append(f"duplicate {kind} ids: {dupes}")
    for key, n in (("nodes", len(nodes)), ("links", len(links)), ("demands", len(demands)),
                   ("events", len(sc.events) + len(sc.change.events if sc.change else []))):
        if n > LIMITS[key]:
            out.append(f"{n} {key}: the cap is {LIMITS[key]}")
    lo, hi = LIMITS["duration_s"]
    if not lo <= sc.duration_s <= hi:
        out.append(f"duration_s {sc.duration_s} is outside {lo}..{hi}")

    def within(name, value, rng, what):
        if value is not None and not rng[0] <= value <= rng[1]:
            out.append(f"{what}: {name} {value} is outside the validated range {rng[0]}..{rng[1]}")

    ns = set(nodes)
    for l in sc.topology.links:
        for end in (l.a, l.z):
            if end not in ns:
                out.append(f"link {l.id}: no node {end!r}")
        if l.a == l.z:
            out.append(f"link {l.id} joins {l.a} to itself")
        within("rate_mbps", l.rate_mbps, LIMITS["rate_mbps"], f"link {l.id}")
        within("delay_ms", l.delay_ms, LIMITS["delay_ms"], f"link {l.id}")
        within("queue_pkts", l.queue_pkts, LIMITS["queue_pkts"], f"link {l.id}")
        within("loss_rate", l.loss_rate, LIMITS["loss_rate"], f"link {l.id}")
    for d in sc.traffic.demands:
        for end in (d.src, d.dst):
            if end not in ns:
                out.append(f"demand {d.id}: no node {end!r}")
        if d.src == d.dst:
            out.append(f"demand {d.id} goes from {d.src} to itself")
        within("rate_mbps", d.rate_mbps, LIMITS["demand_rate_mbps"], f"demand {d.id}")
        within("packet_bytes", d.packet_bytes, LIMITS["packet_bytes"], f"demand {d.id}")
        stop = d.stop_s if d.stop_s is not None else sc.duration_s
        if not 0 <= d.start_s < stop <= sc.duration_s:
            out.append(f"demand {d.id}: start {d.start_s} and stop {stop} must satisfy 0 <= start < stop <= duration")
    ls = set(links)
    for e in [*sc.events, *(sc.change.events if sc.change else [])]:
        if e.target not in ls:
            out.append(f"event at t={e.t}: no link {e.target!r}")
        if not 0 <= e.t < sc.duration_s:
            out.append(f"event at t={e.t} is outside the run (0..{sc.duration_s})")
    added = [d for d in (sc.change.add_demands if sc.change else [])]
    for d in added:
        if d.id in demands:
            out.append(f"change: demand {d.id!r} is added but exists already")
        for end in (d.src, d.dst):
            if end not in ns:
                out.append(f"change: added demand {d.id}: no node {end!r}")
        within("rate_mbps", d.rate_mbps, LIMITS["demand_rate_mbps"], f"added demand {d.id}")
    for r_id in (sc.change.remove_demands if sc.change else []):
        if r_id not in demands:
            out.append(f"change: no demand {r_id!r} to remove")
    ds = set(demands) | {d.id for d in added}
    for r in sc.requirements:
        if r.demand not in ds:
            out.append(f"requirement on {r.kpi}: no demand {r.demand!r}")
    data = sc.model_dump()
    for u in sc.uncertain_params:
        try:
            _resolve(data, u.path)
        except ValueError as e:
            out.append(f"uncertain parameter: {e}")
        if u.dist in ("normal", "lognormal") and (u.mean is None or u.std is None or u.std < 0):
            out.append(f"uncertain {u.path}: {u.dist} needs mean and std >= 0")
        if u.dist == "uniform" and (u.low is None or u.high is None or u.low > u.high):
            out.append(f"uncertain {u.path}: uniform needs low <= high")
    if sc.change:
        for s in sc.change.set:
            try:
                _resolve(data, s.path)
            except ValueError as e:
                out.append(f"change: {e}")
    if sc.seed_policy.max_runs < sc.seed_policy.runs:
        out.append("seed_policy: max_runs is below runs")
    if not sc.traffic.demands:
        out.append("no demands: there is nothing to measure")
    return out


def parse(data: Union[str, Dict[str, Any]]) -> Scenario:
    """A scenario from JSON text or a dict (schema stage). Raises pydantic.ValidationError."""
    import json
    if isinstance(data, str):
        data = json.loads(data)
    return Scenario.model_validate(data)


def baseline_and_changed(sc: Scenario) -> tuple:
    """(baseline dict, changed dict): the scenario without its change, and with it applied."""
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


EXAMPLE = {   # valid as it stands (tests check it): a triangle, a failure, a loss requirement
    "schema_version": "1.0", "domain": "ip", "snapshot_id": "",
    "question": "does acme keep its loss under 1% if L-ab fails at t=10?",
    "topology": {"nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                 "links": [{"id": "L-ab", "a": "a", "z": "b", "rate_mbps": 100, "delay_ms": 5},
                           {"id": "L-ac", "a": "a", "z": "c", "rate_mbps": 50, "delay_ms": 5},
                           {"id": "L-cb", "a": "c", "z": "b", "rate_mbps": 50, "delay_ms": 5}]},
    "traffic": {"demands": [{"id": "acme", "src": "a", "dst": "b", "protocol": "udp", "rate_mbps": 30,
                             "start_s": 1, "stop_s": 30}]},
    "change": {"events": [{"t": 10, "type": "link_down", "target": "L-ab"}]},
    "duration_s": 30,
    "requirements": [{"demand": "acme", "kpi": "loss_ratio", "op": "<", "value": 0.01}],
    "uncertain_params": [{"path": "links.L-ac.delay_ms", "dist": "normal", "mean": 5, "std": 1}],
    "seed_policy": {"runs": 20, "max_runs": 60},
}
