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
    return {"error": f"no domain or snapshot called {name!r}",
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
    return {"snapshot_id": s["snapshot_id"], "domain": s.get("domain"), "age_s": age_s(s),
            "topology": {"nodes": [n for n in t["nodes"] if n["id"] in keep],
                         "links": [l for l in t["links"] if l["a"] in keep and l["z"] in keep]},
            "traffic": {"demands": [d for d in s.get("traffic", {}).get("demands", [])
                                    if d["src"] in keep and d["dst"] in keep]}}
