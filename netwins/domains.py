"""
The twins netwins has, one per network domain, behind one pipeline.

A domain is what changes from one kind of network to another, and nothing else:
  model                 the scenario's Pydantic model (its `domain` field names it)
  problems(sc)          the semantic checks
  baseline_and_changed  the scenario without and with its change
  set_path / clamp      the fields a change may set or an uncertainty may sample, kept physical
  new_entities(sc)      what the change adds (not in the baseline, so not judged there)
  engine(spec)          what runs one member: docker | local:<binary> | fake (tests)
  EXAMPLE, NOT_MODELLED, FIDELITY, skill

The ensemble, aggregation, calibration, policy, envelope, cache and the CANAH provider are shared: adding a
domain is a model, an engine and a skill (the plan's reuse test).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Union

from pydantic import ValidationError


@dataclass
class Domain:
    name: str
    title: str
    model: Any
    problems: Callable[[Any], List[str]]
    baseline_and_changed: Callable[[Any], tuple]
    set_path: Callable[[Dict[str, Any], str, Any], None]
    clamp: Callable[[str, float], float]
    resolve: Callable[[Dict[str, Any], str], tuple]
    new_entities: Callable[[Any], set]
    engine: Callable[[str], Any]
    example: Dict[str, Any]
    not_modelled: str
    fidelity: str
    skill: str
    engine_version: str


def _ip() -> Domain:
    from . import schema
    from .runner import DockerEngine, FakeEngine, LocalEngine

    def engine(spec: str):
        if spec == "fake":
            return FakeEngine()
        if spec.startswith("local:"):
            return LocalEngine(spec.split(":", 1)[1])
        return DockerEngine()

    return Domain(
        name="ip", title="IP / packet", model=schema.Scenario, problems=schema.problems,
        baseline_and_changed=schema.baseline_and_changed, set_path=schema.set_path,
        clamp=_ip_clamp, resolve=schema._resolve,
        new_entities=lambda sc: {d.id for d in sc.change.add_demands} if sc.change else set(),
        engine=engine, example=schema.EXAMPLE,
        not_modelled=("Protocols and traffic are modelled, not router software; one domain only: effects in other "
                      "domains (optical, wireless) are not modelled."),
        fidelity="packet-level (ns-3 point-to-point, global routing)", skill="ip-twin",
        engine_version="netwins-ip 0.2")


def _ip_clamp(path: str, v: float) -> float:
    field = path.rsplit(".", 1)[-1]
    if field == "loss_rate":
        return min(max(v, 0.0), 0.5)
    if field == "queue_pkts":
        return max(1, int(round(v)))
    return max(v, 0.0)


def _optical() -> Domain:
    from . import optical as o

    def engine(spec: str):
        return o.OpticalEngine()          # GNPy is fast and local: tests use the real thing

    return Domain(
        name="optical", title="Optical (WDM)", model=o.OScenario, problems=o.problems,
        baseline_and_changed=o.baseline_and_changed, set_path=o.set_path, clamp=o.clamp, resolve=o.resolve,
        new_entities=o.new_entities, engine=engine, example=o.EXAMPLE, not_modelled=o.NOT_MODELLED,
        fidelity=o.FIDELITY, skill="optical-twin", engine_version=o.OpticalEngine.version())


def _wifi() -> Domain:
    from . import wifi as w
    from .runner import DockerEngine, LocalEngine

    def engine(spec: str):
        if spec == "fake":
            return w.FakeWifiEngine()
        if spec.startswith("local:"):
            return LocalEngine(spec.split(":", 1)[1], view=w.engine_view)
        return DockerEngine(program="netwins-wifi", view=w.engine_view)

    return Domain(
        name="wifi", title="Wi-Fi", model=w.WScenario, problems=w.problems,
        baseline_and_changed=w.baseline_and_changed, set_path=w.set_path, clamp=w.clamp, resolve=w.resolve,
        new_entities=w.new_entities, engine=engine, example=w.EXAMPLE, not_modelled=w.NOT_MODELLED,
        fidelity=w.FIDELITY, skill="wifi-twin", engine_version="netwins-wifi 0.1")


def _ntn() -> Domain:
    from . import ntn as n

    def engine(spec: str):
        return n.NTNEngine()              # geometry and link budgets: fast and local, tests use the real thing

    return Domain(
        name="ntn", title="Non-terrestrial (satellite / HAPS)", model=n.NScenario, problems=n.problems,
        baseline_and_changed=n.baseline_and_changed, set_path=n.set_path, clamp=n.clamp, resolve=n.resolve,
        new_entities=n.new_entities, engine=engine, example=n.EXAMPLE, not_modelled=n.NOT_MODELLED,
        fidelity=n.FIDELITY, skill="ntn-twin", engine_version=n.NTNEngine.version())


_FACTORIES = {"ip": _ip, "optical": _optical, "wifi": _wifi, "ntn": _ntn}
_CACHE: Dict[str, Domain] = {}


def names() -> List[str]:
    return list(_FACTORIES)


def get(name: str) -> Domain:
    if name not in _FACTORIES:
        raise ValueError(f"no twin for domain {name!r}; there are {names()}")
    if name not in _CACHE:
        _CACHE[name] = _FACTORIES[name]()
    return _CACHE[name]


def domain_of(data: Union[str, Dict[str, Any], Any]) -> str:
    if hasattr(data, "domain"):
        return data.domain
    if isinstance(data, str):
        import json
        data = json.loads(data)
    return str((data or {}).get("domain") or "ip")


def parse(data: Union[str, Dict[str, Any]]):
    """A scenario of any domain (schema stage). Raises pydantic.ValidationError or ValueError."""
    import json
    if isinstance(data, str):
        data = json.loads(data)
    return get(domain_of(data)).model.model_validate(data)


def default_spec() -> str:
    return os.getenv("NETWINS_ENGINE", "docker")
