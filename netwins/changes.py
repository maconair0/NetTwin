"""
A proposed controller write, as a twin scenario: what would be different, on the live network as the
mirror last read it.

  translate(snapshot, verb, tool, args, answers)
    -> {"modelled": True,  "scenario": {...}, "assumptions": [...]}
    or {"modelled": False, "questions": [...]}       something the twin needs is not in the write: ask
                                                     the operator, then call again with `answers`
    or {"modelled": False, "not_modelled": "..."}    the twin cannot represent this kind of change

By contract verb (source.WRITES):
  provision         a new demand between the service's two ends, at its rate
  teardown          the service's demand removed
  adjust            the service's demand at its new rate
  configure_device  a port or interface shut: that link fails; a port speed: that link's rate
  reroute           not modelled (the twin routes on shortest paths)

`answers` fills what the write leaves out: {"src", "dst", "rate_mbps", "service", "link", "requirements"}.
Requirements default to every service keeping loss under 1%, the new one included; the operator's own
go in `answers.requirements`.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from .mirror import _A_KEYS, _Z_KEYS, endpoint, number_with_unit, safe_id, split_port

DEFAULT_LOSS = 0.01
DURATION_S = 20.0
NAME_KEYS = ("name", "service", "service_name", "id", "service_id", "intent_id", "key", "uuid", "flow_id")


def _resolve(snap: Dict[str, Any], raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw, _ = endpoint(raw, {}, "")
    raw = str(raw)
    nodes = {n["id"] for n in snap["topology"]["nodes"]}
    aliases = snap.get("aliases") or {}
    for cand in (raw, split_port(raw, set(aliases) | nodes)[0]):
        if cand in nodes:
            return cand
        if cand in aliases:
            return aliases[cand]
        if safe_id(cand) in nodes:
            return safe_id(cand)
    return None


def _service(snap, args, answers) -> Optional[str]:
    raw = answers.get("service") or next((args[k] for k in NAME_KEYS if args.get(k) not in (None, "")), None)
    if raw is None:
        return None
    ids = {d["id"] for d in snap["traffic"]["demands"]}
    for cand in (str(raw), safe_id(raw)):
        if cand in ids:
            return cand
    return None


def _link_at(snap, device: str, port: Optional[str]) -> Optional[str]:
    """The link at a device's port, from the ends the mirror recorded; without a port, the device's only
    link. None when it cannot tell."""
    raw = {v: k for k, v in (snap.get("aliases") or {}).items()}.get(device, device)
    if port is not None:
        hits = [lid for lid, ends in (snap.get("ports") or {}).items()
                if any(str(d) in (raw, device) and p is not None and str(p) == str(port) for d, p in ends)]
        return hits[0] if len(hits) == 1 else None
    hits = [l["id"] for l in snap["topology"]["links"] if device in (l["a"], l["z"])]
    return hits[0] if len(hits) == 1 else None


def _requirements(snap, extra_ids: List[str], answers) -> List[Dict[str, Any]]:
    if answers.get("requirements"):
        return list(answers["requirements"])
    ids = [d["id"] for d in snap["traffic"]["demands"] if not d["id"].startswith("bg.")] + extra_ids
    return [{"demand": i, "kpi": "loss_ratio", "op": "<", "value": DEFAULT_LOSS} for i in dict.fromkeys(ids)]


def _scenario(snap, change, reqs, question) -> Dict[str, Any]:
    demands = []
    for d in copy.deepcopy(snap["traffic"]["demands"]):
        d.setdefault("start_s", 1.0)
        d["stop_s"] = None
        demands.append(d)
    return {"schema_version": "1.0", "domain": "ip", "snapshot_id": snap["snapshot_id"], "question": question,
            "topology": copy.deepcopy(snap["topology"]), "traffic": {"demands": demands}, "change": change,
            "duration_s": DURATION_S, "requirements": reqs,
            "seed_policy": {"runs": 10, "max_runs": 40, "batch": 10, "adaptive": True}}


def translate(snap: Dict[str, Any], verb: Optional[str], tool: str, args: Dict[str, Any],
              answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    answers = dict(answers or {})
    args = dict(args or {})
    what = f"{tool}({', '.join(f'{k}={v!r}' for k, v in list(args.items())[:6])})"
    assumptions: List[str] = []

    if verb == "provision":
        a = answers.get("src") or next((args[k] for k in _A_KEYS if args.get(k) not in (None, "")), None)
        z = answers.get("dst") or next((args[k] for k in _Z_KEYS if args.get(k) not in (None, "")), None)
        if (a is None or z is None) and isinstance(args.get("endpoints"), list) and len(args["endpoints"]) >= 2:
            a, z = args["endpoints"][0], args["endpoints"][1]
        na, nz = _resolve(snap, a), _resolve(snap, z)
        rate = answers.get("rate_mbps")
        if rate is None:
            rate, _ = number_with_unit(args, ("bandwidth", "rate", "capacity", "mbps"))
        questions = []
        if not na or not nz:
            lost = [repr(x) for x, n in ((a, na), (z, nz)) if x is not None and not n]
            questions.append(f"{what} does not name two ends the twin can find"
                             + (f" ({', '.join(lost)} not in the mirrored topology)" if lost else "")
                             + ": which two nodes or hosts will this service connect? (answers.src, answers.dst)")
        if rate is None:
            questions.append(f"{what} gives no rate: how much traffic will it carry, in Mbit/s? (answers.rate_mbps)")
        if questions:
            return {"modelled": False, "verb": verb, "questions": questions}
        name = safe_id(next((args[k] for k in NAME_KEYS if args.get(k) not in (None, "")), "new-service"))
        if any(d["id"] == name for d in snap["traffic"]["demands"]):
            name = safe_id(name + ".new")
        change = {"add_demands": [{"id": name, "src": na, "dst": nz, "protocol": "udp", "rate_mbps": float(rate),
                                   "start_s": 1.0}]}
        reqs = _requirements(snap, [name], answers)
        return {"modelled": True, "verb": verb, "assumptions": assumptions,
                "scenario": _scenario(snap, change, reqs, f"does every service keep its requirement if {what} runs?")}

    if verb in ("teardown", "adjust"):
        sid = _service(snap, args, answers)
        if not sid:
            return {"modelled": False, "verb": verb, "questions": [
                f"{what}: which service is this? None of its arguments names a service the mirror read "
                f"({', '.join(d['id'] for d in snap['traffic']['demands'] if not d['id'].startswith('bg.'))[:300]}) "
                f"(answers.service)"]}
        if verb == "teardown":
            change = {"remove_demands": [sid]}
            reqs = [r for r in _requirements(snap, [], answers) if r["demand"] != sid]
            if not reqs:
                return {"modelled": False, "verb": verb, "not_modelled":
                        f"removing {sid} leaves no other service to check: nothing for the twin to predict"}
        else:
            rate = answers.get("rate_mbps")
            if rate is None:
                rate, _ = number_with_unit(args, ("bandwidth", "rate", "capacity", "mbps"))
            if rate is None:
                return {"modelled": False, "verb": verb, "questions": [
                    f"{what}: the twin models a service change as a new rate; what rate will {sid} carry, in "
                    f"Mbit/s? (answers.rate_mbps). Other adjustments are not modelled."]}
            change = {"set": [{"path": f"demands.{sid}.rate_mbps", "value": float(rate)}]}
            reqs = _requirements(snap, [], answers)
        return {"modelled": True, "verb": verb, "assumptions": assumptions,
                "scenario": _scenario(snap, change, reqs, f"does every service keep its requirement if {what} runs?")}

    if verb == "configure_device":
        dev = _resolve(snap, next((args[k] for k in ("device", "device_id", "deviceId", "dpid", "node", "router",
                                                      "switch", "name") if args.get(k)), None))
        port = next((str(args[k]) for k in ("port", "port_no", "interface", "ifname") if args.get(k) is not None), None)
        text = " ".join(f"{k}={v}" for k, v in args.items()).lower()
        shut = any(w in text for w in ("shutdown", "admin_down", "disable", "enabled=false", "state=down",
                                       "admin-state=disable", "status=down", "=down"))
        speed, _ = number_with_unit({k: v for k, v in args.items() if k not in ("port", "port_no")},
                                    ("speed", "rate", "bandwidth"))
        link = answers.get("link") or (_link_at(snap, dev, port) if dev else None)
        if not (shut or speed):
            return {"modelled": False, "verb": verb, "not_modelled":
                    f"{what}: the twin models a device setting only as a port shut (the link fails) or a port speed "
                    f"(the link's rate); this one is neither"}
        if not link:
            return {"modelled": False, "verb": verb, "questions": [
                f"{what}: which link is at {dev or args}{(' port ' + port) if port else ''}? The twin cannot tell "
                f"from the mirror (answers.link)"]}
        change = ({"events": [{"t": 0.5, "type": "link_down", "target": link}]} if shut else
                  {"set": [{"path": f"links.{link}.rate_mbps", "value": float(speed)}]})
        return {"modelled": True, "verb": verb, "assumptions": assumptions,
                "scenario": _scenario(snap, change, _requirements(snap, [], answers),
                                      f"does every service keep its requirement if {what} runs?")}

    if verb == "reroute":
        return {"modelled": False, "verb": verb, "not_modelled":
                "a reroute pins a path; the twin routes on shortest paths, so it cannot predict it"}
    return {"modelled": False, "verb": verb, "not_modelled":
            f"{tool} serves no contract verb the twin models (provision, teardown, adjust, configure_device)"}
