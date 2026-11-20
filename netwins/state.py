"""
The network's state as the twin sees it: canonical snapshots, read-only.

A snapshot is one JSON file in the snapshots folder (NETWINS_SNAPSHOTS, default data/snapshots):
{snapshot_id, domain, taken_at (epoch s, or null), source, topology {nodes, links}, traffic {demands}}.
An exporter from the controllers (CANAH's agentic MCP, a DB) writes these; the twin only reads them.

  domains()                     the domains with snapshots, and their latest snapshot
  latest(domain)                the newest snapshot of a domain
  get(snapshot_id)              one snapshot
  inspect(domain)               a summary: counts, ids, age
  subnetwork(domain, anchors, hops)   the nodes within `hops` of the anchors, the links among them, and the
                                demands between them: the slice a scenario needs
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent


def folder() -> Path:
    return Path(os.getenv("NETWINS_SNAPSHOTS", str(ROOT / "data" / "snapshots")))


def _all() -> List[Dict[str, Any]]:
    out = []
    for p in sorted(folder().glob("*.json")):
        try:
            s = json.loads(p.read_text())
            s.setdefault("snapshot_id", p.stem)
            if s.get("taken_at") is None:
                s["taken_at"] = p.stat().st_mtime
            out.append(s)
        except (OSError, ValueError):
            continue
    return out


def domains() -> Dict[str, str]:
    """{network or domain: its latest snapshot id}. A mirrored network is named by its controller."""
    out: Dict[str, Any] = {}
    for s in sorted(_all(), key=lambda s: s["taken_at"]):
        out[s.get("network") or s.get("domain", "?")] = s["snapshot_id"]
    return out


def get(snapshot_id: str) -> Optional[Dict[str, Any]]:
    return next((s for s in _all() if s["snapshot_id"] == snapshot_id), None)


def latest(domain: str) -> Optional[Dict[str, Any]]:
    found = [s for s in _all() if domain in (s.get("network"), s.get("domain"))]
    return max(found, key=lambda s: s["taken_at"]) if found else None


def age_s(snap: Dict[str, Any]) -> float:
    return round(time.time() - float(snap.get("taken_at") or 0), 1)


def find(name: str) -> Optional[Dict[str, Any]]:
    """A domain's latest snapshot, or a snapshot by its id: callers mix the two up."""
    return latest(name) or get(name)


def _missing(name: str) -> Dict[str, Any]:
    return {"error": f"no snapshot called {name!r} (a twin needs no snapshot: when the network is described in "
                     f"the question, build the scenario from that, with twin_schema(domain)'s example)",
            "available": [{"domain": d, "snapshot_id": sid} for d, sid in domains().items()]}


def inspect(domain: str) -> Dict[str, Any]:
    s = find(domain)
    if s is None:
        return _missing(domain)
    t = s["topology"]
    return {"snapshot_id": s["snapshot_id"], "domain": s.get("domain"), "age_s": age_s(s), "source": s.get("source"),
            "nodes": [n["id"] for n in t["nodes"]],
            "links": [{"id": l["id"], "a": l["a"], "z": l["z"], "rate_mbps": l.get("rate_mbps"),
                       "delay_ms": l.get("delay_ms")} for l in t["links"]],
            "demands": [{"id": d["id"], "src": d["src"], "dst": d["dst"], "rate_mbps": d.get("rate_mbps"),
                         "protocol": d.get("protocol", "udp")} for d in s.get("traffic", {}).get("demands", [])]}


def subnetwork(domain: str, anchors: List[str], hops: int = 2) -> Dict[str, Any]:
    s = find(domain)
    if s is None:
        return _missing(domain)
    t = s["topology"]
    # A link id as an anchor stands for its two ends; no anchors means the whole network.
    ends = {l["id"]: (l["a"], l["z"]) for l in t["links"]}
    anchors = [x for a in anchors for x in (ends[a] if a in ends else (a,))] or [n["id"] for n in t["nodes"]]
    adj: Dict[str, set] = {}
    for l in t["links"]:
        adj.setdefault(l["a"], set()).add(l["z"])
        adj.setdefault(l["z"], set()).add(l["a"])
    unknown = [a for a in anchors if a not in adj]
    if unknown:
        return {"error": f"no node {unknown} in snapshot {s['snapshot_id']}"}
    keep, frontier = set(anchors), set(anchors)
    for _ in range(max(0, int(hops))):
        frontier = {m for n in frontier for m in adj.get(n, ())} - keep
        keep |= frontier
    # Traffic that crosses the slice is part of it: a demand whose path touches the slice comes in with its
    # whole path (and every path it could take round the slice's links), or the slice would show its links
    # emptier than they are. Only demands that never touch it are left out, and they are named.
    demands = s.get("traffic", {}).get("demands", [])
    taken, left = [], []
    for d in demands:
        if d["src"] not in adj or d["dst"] not in adj:
            left.append(d["id"])
            continue
        routes = _paths(adj, d["src"], d["dst"])
        if any(set(r) & keep for r in routes):
            for r in routes:
                keep |= set(r)
            taken.append(d["id"])
        else:
            left.append(d["id"])
    return {"snapshot_id": s["snapshot_id"], "domain": s.get("domain"), "age_s": age_s(s),
            "topology": {"nodes": [n for n in t["nodes"] if n["id"] in keep],
                         "links": [l for l in t["links"] if l["a"] in keep and l["z"] in keep]},
            "traffic": {"demands": [d for d in demands if d["id"] in taken]},
            "left_out": {"demands": left, "why": "they never touch this slice"} if left else {}}


def _paths(adj: Dict[str, set], src: str, dst: str, limit: int = 8, max_len: int = 12) -> List[List[str]]:
    """Up to `limit` loop-free paths from src to dst, shortest first: where a demand could run, including
    round a failure."""
    out: List[List[str]] = []
    queue = [[src]]
    while queue and len(out) < limit:
        path = queue.pop(0)
        if path[-1] == dst:
            out.append(path)
            continue
        if len(path) > max_len:
            continue
        for m in sorted(adj.get(path[-1], ())):
            if m not in path:
                queue.append(path + [m])
    return out
