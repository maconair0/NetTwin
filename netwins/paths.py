"""
Paths into a scenario, the same way for every domain:

  "<kind>.<id>.<field>"   one object of a list:  links.L2.rate_mbps, lightpaths.LP-1.tx_power_dbm
  "<kind>.<field>"        a single object:       radio.path_loss_exponent

`settable` says which fields of each kind may be set by a change or sampled as uncertain; `where` says
whether a kind lives under "topology", "traffic" or at the top of the scenario ("").
"""
from __future__ import annotations

import re
from typing import Any, Dict, Set, Tuple

_THREE = re.compile(r"^([a-z_]+)\.([A-Za-z0-9_.:-]+?)\.([a-z_]+)$")
_TWO = re.compile(r"^([a-z_]+)\.([a-z_]+)$")


def resolve(data: Dict[str, Any], path: str, settable: Dict[str, Set[str]], where: Dict[str, str]) -> Tuple[dict, str]:
    """(the object, field) a path points at; ValueError when it points nowhere or at a field that is fixed."""
    path = path or ""
    m2 = _TWO.match(path)
    if m2 and m2.group(1) in settable and not isinstance(_container(data, m2.group(1), where), list):
        kind, field = m2.groups()
        _allowed(path, kind, field, settable)
        obj = _container(data, kind, where)
        if not isinstance(obj, dict):
            raise ValueError(f"{path!r}: the scenario has no {kind}")
        return obj, field
    m = _THREE.match(path)
    if not m:
        kinds = ", ".join(sorted(settable))
        raise ValueError(f"{path!r} is not a path like <kind>.<id>.<field> (kinds: {kinds})")
    kind, ident, field = m.groups()
    if kind not in settable:
        raise ValueError(f"{path!r}: {kind} cannot be set or sampled (kinds: {', '.join(sorted(settable))})")
    _allowed(path, kind, field, settable)
    items = _container(data, kind, where)
    for obj in items or []:
        if obj.get("id") == ident:
            return obj, field
    raise ValueError(f"{path!r}: no {kind.rstrip('s')} {ident!r}")


def _allowed(path: str, kind: str, field: str, settable: Dict[str, Set[str]]) -> None:
    if field not in settable.get(kind, set()):
        raise ValueError(f"{path!r}: {kind} field {field!r} cannot be set or sampled "
                         f"(allowed: {sorted(settable.get(kind, set()))})")


def _container(data: Dict[str, Any], kind: str, where: Dict[str, str]) -> Any:
    place = where.get(kind, "topology")
    holder = data.get(place, {}) if place else data
    return (holder or {}).get(kind)
