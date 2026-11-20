"""
A twin of the live network: read a controller through the agentic MCP and write a snapshot the twin
runs on.

  mirror(source, key)   devices, links, hosts, services and, when the controller has counters, two
                        reads of them `interval_s` apart: the traffic on each link now
  -> snapshot           {snapshot_id, domain "ip", network <key>, taken_at, topology, traffic,
                         provenance, coverage, aliases}

Controllers answer in their own shapes (ONOS, Ryu, an emulator...). The reading here is tolerant and
records what it did: every value is either *measured* (the controller said it, or the counters showed
it) or *assumed* (a default, because the controller does not say). `coverage` counts both; a twin
built mostly on assumptions says so in every prediction, and the policy will not let a change go
ahead on it (see `trust_caps`).

Traffic: each service with two ends becomes a demand (its id is the service's). The counters give what
each link carries now; what the services do not account for becomes a one-hop background demand on
that link, so the twin runs with the load the network has, not an empty one.
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import source as src

DEFAULT_RATE_MBPS = 1000.0       # a link whose capacity the controller does not give
DEFAULT_DELAY_MS = 1.0           # a link whose latency it does not give
DEFAULT_DEMAND_MBPS = 1.0        # a service whose bandwidth it does not give
HOST_LINK_MBPS = 1000.0
MIN_BACKGROUND_MBPS = 0.05
MAX_PORT_READS = 60              # per-device calls (ports, counters) at most

_ID = re.compile(r"[^A-Za-z0-9_.:-]")

_DEV_KEYS = ("id", "device_id", "deviceId", "dpid", "name", "node_id", "node-id", "uuid", "router", "switch")
_A_KEYS = ("src", "source", "a", "a_end", "aEnd", "from", "src_device", "one", "ingress", "ingressPoint",
           "local", "left")
_Z_KEYS = ("dst", "destination", "target", "z", "z_end", "zEnd", "to", "dst_device", "two", "egress",
           "egressPoint", "remote", "right")
_PORT_KEYS = ("port", "port_no", "portNumber", "interface", "ifname", "endpoint", "port_id")
_TX_KEYS = ("bytesSent", "tx_bytes", "out_octets", "tx_octets", "txBytes", "bytes_out", "bytesTx", "out_bytes")
_RX_KEYS = ("bytesReceived", "rx_bytes", "in_octets", "rx_octets", "rxBytes", "bytes_in", "bytesRx", "in_bytes")


def _canon(raw: Any) -> Optional[int]:
    """A datapath id's number, however it is written: 1, "1", "0000000000000001", "of:0000000000000001"."""
    t = str(raw)
    t = t[3:] if t.startswith("of:") else t
    if re.fullmatch(r"[0-9a-fA-F]{16}", t):
        return int(t, 16)
    if re.fullmatch(r"[0-9]{1,15}", t):
        return int(t)
    return None


def safe_id(raw: Any) -> str:
    s = _ID.sub("_", str(raw))[:64]
    return s or "_"


# ---------------------------------------------------------------- reading shapes

def rows(data: Any, prefer: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The list of records in a controller's answer: a list, the biggest list inside a dict, or a dict
    of records keyed by id."""
    if isinstance(data, str):
        data = src.parse(data)
    if isinstance(data, list):
        return [r if isinstance(r, dict) else {"id": r} for r in data]
    if not isinstance(data, dict):
        return []
    for k in prefer:
        if isinstance(data.get(k), list):
            return rows(data[k])
    lists = [v for v in data.values() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]
    if lists:
        return max(lists, key=len)
    if data and all(isinstance(v, dict) for v in data.values()):
        return [{"id": k, **v} for k, v in data.items()]
    return [data] if data else []


def _scalar(v: Any) -> Optional[str]:
    if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v) != "":
        return str(v)
    if isinstance(v, dict):
        for k in (*_DEV_KEYS, "elementId", "device"):
            if k in v:
                got = _scalar(v[k])
                if got:
                    return got
    return None


def _first(row: Dict[str, Any], keys: Iterable[str]) -> Any:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def number_with_unit(row: Dict[str, Any], words: Iterable[str]) -> Tuple[Optional[float], str]:
    """A value in Mbit/s (or ms) from the first key that names it, scaled by the unit in the key."""
    scale = {"gbps": 1000.0, "gbit": 1000.0, "mbps": 1.0, "mbit": 1.0, "kbps": 0.001, "kbit": 0.001,
             "bps": 1e-6, "_us": 0.001, "usec": 0.001, "_ms": 1.0, "msec": 1.0, "_ns": 1e-6, "_s": 1000.0}
    for key, v in row.items():
        lk = key.lower()
        if not any(w in lk for w in words) or isinstance(v, bool):
            continue
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        for suffix, f in scale.items():
            if lk.endswith(suffix) or suffix.strip("_") == lk.split("_")[-1]:
                return x * f, f"{key} ({suffix.strip('_')})"
        if lk == "portspeed":                       # ONOS: Mbit/s
            return x, key
        return x, f"{key} (no unit given: read as Mbit/s or ms)"
    return None, ""


def endpoint(v: Any, row: Dict[str, Any], side: str) -> Tuple[Optional[str], Optional[str]]:
    """(device, port) of one end of a link or service."""
    if isinstance(v, dict):
        dev = _scalar({k: v[k] for k in v if k not in _PORT_KEYS})
        port = _first(v, _PORT_KEYS)
        return dev, None if port is None else str(port)
    if v is None:
        return None, None
    s = str(v)
    port = row.get(f"{side}_port") or row.get(f"{side}Port")
    return s, None if port is None else str(port)


def split_port(raw: str, devices: set) -> Tuple[str, Optional[str]]:
    """'pe-a:eth1' -> ('pe-a', 'eth1') when 'pe-a' is a device; ONOS ids have colons of their own."""
    if raw in devices:
        return raw, None
    for sep in ("/", ":"):
        if sep in raw:
            head, tail = raw.rsplit(sep, 1)
            if head in devices:
                return head, tail
    return raw, None


# ---------------------------------------------------------------- the mirror

class _Snap:
    def __init__(self, key: str):
        self.key = key
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.aliases: Dict[str, str] = {}
        self.links: List[Dict[str, Any]] = []
        self.demands: List[Dict[str, Any]] = []
        self.prov: List[str] = []
        self.count = {"links": 0, "link_rate_measured": 0, "link_delay_measured": 0, "services": 0,
                      "services_modelled": 0, "service_rate_measured": 0, "links_with_traffic_measured": 0,
                      "background_demands": 0}

    def node(self, raw: str, role: str = "") -> str:
        if raw in self.aliases:
            return self.aliases[raw]
        canon = _canon(raw)
        if canon is not None:
            for known, nid in self.aliases.items():
                if _canon(known) == canon:          # one switch, named two ways (Ryu: 1 and 0000000000000001)
                    self.aliases[raw] = nid
                    return nid
        nid = safe_id(raw)
        while nid in self.nodes and self.nodes[nid]["raw"] != raw:
            nid = safe_id(nid + "_")
        self.nodes[nid] = {"id": nid, "role": role, "raw": raw}
        self.aliases[raw] = nid
        return nid


def mirror(source: Any, key: str, interval_s: float = 5.0, sleep=time.sleep) -> Dict[str, Any]:
    row = src.controller(source, key)
    domain = str(row.get("domain") or "").lower()
    if domain and domain not in ("packet", "ip", "sdn", "l2", "l3"):
        # The mirror builds the IP twin's picture of a packet network. An optical controller's fibres and
        # lightpaths are not that: filing them as an IP network only shadowed the real IP snapshots.
        raise ValueError(f"{key} is a {domain} controller: the mirror builds IP snapshots of packet controllers. For a "
                         f"what-if on it, describe its network in a scenario of the matching twin (twin_schema('optical') "
                         f"for fibres and lightpaths)")
    tools = source.tools(key)
    v = src.verbs(row, tools)
    snap = _Snap(key)
    reads: Dict[str, str] = {}

    def read(verb: str, args: Optional[Dict[str, Any]] = None) -> Any:
        reads[verb] = v[verb]
        return src.parse(source.call(key, v[verb], args or {}))

    if "read_links" not in v:
        raise ValueError(f"{key} serves no read_links verb: the twin cannot know how the network is connected")

    # Devices
    if "read_devices" in v and not src.required_args(tools, v["read_devices"]):
        for r in rows(read("read_devices"), ("devices", "switches", "nodes", "routers")):
            d = _scalar(r) or _scalar({k: r[k] for k in r if k in _DEV_KEYS})
            if d:
                snap.node(d, str(r.get("role") or r.get("type") or "device").lower()[:24])
    devices = set(snap.aliases)

    # Links (and hosts, when the topology read carries them)
    topo = read("read_links")
    seen = set()
    link_rows = rows(topo, ("links",))
    for r in link_rows:
        (a, ap), (z, zp) = endpoint(_first(r, _A_KEYS), r, "a"), endpoint(_first(r, _Z_KEYS), r, "z")
        if not a or not z:
            continue
        if ap is None:
            a, ap = split_port(a, devices)
        if zp is None:
            z, zp = split_port(z, devices)
        state = str(r.get("state") or r.get("status") or "up").lower()
        sig = frozenset([(a, ap), (z, zp)])
        if sig in seen:
            continue                                  # the other direction of a link already read
        seen.add(sig)
        if state in ("down", "inactive", "disabled", "failed"):
            snap.prov.append(f"link {a}-{z} left out: the controller reports it {state}")
            continue
        na, nz = snap.node(a), snap.node(z)
        rate, rate_from = number_with_unit(r, ("rate", "capacity", "bandwidth", "speed"))
        delay, delay_from = number_with_unit(r, ("latency", "delay"))
        lid = safe_id(r.get("name") or r.get("id") or f"{na}-{nz}")
        if any(l["id"] == lid for l in snap.links):
            lid = safe_id(f"{lid}.{len(snap.links)}")
        link = {"id": lid, "a": na, "z": nz, "rate_mbps": rate, "delay_ms": delay, "queue_pkts": 100,
                "_ports": (ap, zp), "_raw": (a, z)}
        snap.count["links"] += 1
        if rate is not None:
            snap.count["link_rate_measured"] += 1
            snap.prov.append(f"link {lid}: rate {rate:g} Mbit/s from the controller ({rate_from})")
        if delay is not None:
            snap.count["link_delay_measured"] += 1
        snap.links.append(link)

    # Link capacity from port speeds, when links do not carry it (ONOS)
    if any(l["rate_mbps"] is None for l in snap.links) and "read_ports" in v:
        need = src.required_args(tools, v["read_ports"])
        speed: Dict[Tuple[str, str], float] = {}
        for raw in list(devices)[:MAX_PORT_READS]:
            try:
                got = read("read_ports", {need[0]: raw} if need else {})
            except RuntimeError:
                continue
            for p in rows(got, ("ports",)):
                s, _ = number_with_unit(p, ("speed", "rate", "capacity"))
                port = _first(p, ("port", "port_no", "number", "name", "interface"))
                if s and port is not None:
                    speed[(raw, str(port))] = s
            if not need:
                break
        for l in snap.links:
            if l["rate_mbps"] is None:
                got = [speed.get((l["_raw"][0], l["_ports"][0])), speed.get((l["_raw"][1], l["_ports"][1]))]
                got = [g for g in got if g]
                if got:
                    l["rate_mbps"] = min(got)
                    snap.count["link_rate_measured"] += 1
                    snap.prov.append(f"link {l['id']}: rate {min(got):g} Mbit/s from its port speeds")

    # Hosts: a node and an access link each
    host_rows = []
    if isinstance(topo, dict) and isinstance(topo.get("hosts"), list):
        host_rows = topo["hosts"]
    elif "read_hosts" in v and v["read_hosts"] != v["read_links"] and not src.required_args(tools, v["read_hosts"]):
        try:
            host_rows = rows(read("read_hosts"), ("hosts",))
        except RuntimeError:
            host_rows = []
    for h in host_rows:
        hid = next((str(h[k]) for k in ("id", "mac", "name") if h.get(k) not in (None, "")), "")
        loc = h.get("locations") or h.get("location") or h.get("port") or {}
        loc = loc[0] if isinstance(loc, list) and loc else loc
        dev, port = endpoint(loc, h, "") if isinstance(loc, dict) else (None, None)
        if not hid or not dev:
            continue
        nh, nd = snap.node(hid, "host"), snap.node(dev)
        for ip in (h.get("ipAddresses") or h.get("ipv4") or []):
            snap.aliases.setdefault(str(ip), nh)
        snap.links.append({"id": safe_id(f"acc-{nh}")[:64], "a": nh, "z": nd, "rate_mbps": None, "delay_ms": None,
                           "queue_pkts": 100, "_ports": (None, port), "_raw": (hid, dev), "_access": True})

    # Defaults for what the controller did not say
    assumed_rate = [l["id"] for l in snap.links if l["rate_mbps"] is None]
    assumed_delay = [l["id"] for l in snap.links if l["delay_ms"] is None]
    for l in snap.links:
        if l["rate_mbps"] is None:
            l["rate_mbps"] = HOST_LINK_MBPS if l.get("_access") else DEFAULT_RATE_MBPS
        if l["delay_ms"] is None:
            l["delay_ms"] = DEFAULT_DELAY_MS
    if assumed_rate:
        snap.prov.append(f"assumed: {len(assumed_rate)} link(s) without a capacity from the controller run at "
                         f"{DEFAULT_RATE_MBPS:g} Mbit/s ({', '.join(assumed_rate[:8])}{'...' if len(assumed_rate) > 8 else ''})")
    if assumed_delay:
        snap.prov.append(f"assumed: {len(assumed_delay)} link(s) without a latency run at {DEFAULT_DELAY_MS:g} ms")

    # Services: demands
    if "read_services" in v and not src.required_args(tools, v["read_services"]):
        for r in rows(read("read_services"), ("intents", "services", "flows")):
            snap.count["services"] += 1
            a, z = _first(r, _A_KEYS), _first(r, _Z_KEYS)
            if (a is None or z is None) and isinstance(r.get("endpoints"), list) and len(r["endpoints"]) >= 2:
                a, z = r["endpoints"][0], r["endpoints"][1]
            (da, _), (dz, _) = endpoint(a, r, "a"), endpoint(z, r, "z")
            if not da or not dz:
                continue
            da, dz = split_port(da, set(snap.aliases))[0], split_port(dz, set(snap.aliases))[0]
            if da not in snap.aliases or dz not in snap.aliases:
                snap.prov.append(f"service {r.get('id') or r.get('name')}: ends {da}, {dz} are not in the topology; "
                                 f"left out")
                continue
            rate, rate_from = number_with_unit(r, ("bandwidth", "rate", "capacity"))
            sid = safe_id(r.get("id") or r.get("name") or r.get("key") or f"svc{snap.count['services']}")
            if rate is None:
                rate = DEFAULT_DEMAND_MBPS
                snap.prov.append(f"assumed: service {sid} carries {rate:g} Mbit/s (the controller gives no rate; "
                                 f"the counters below may add the rest as background)")
            else:
                snap.count["service_rate_measured"] += 1
            snap.demands.append({"id": sid, "src": snap.aliases[da], "dst": snap.aliases[dz], "protocol": "udp",
                                 "rate_mbps": round(rate, 4), "start_s": 1.0})
            snap.count["services_modelled"] += 1

    # Telemetry: what each link carries now, from two counter reads
    if "read_statistics" in v:
        try:
            first, t0 = _counters(read, tools, v, devices), time.monotonic()
            sleep(interval_s)
            second, t1 = _counters(read, tools, v, devices), time.monotonic()
            _background(snap, first, second, max(t1 - t0, 1e-3))
        except RuntimeError as e:
            snap.prov.append(f"no traffic counters: {e}")
    else:
        snap.prov.append("no traffic counters: the controller serves no read_statistics verb; the twin runs "
                         "only the services' own rates")

    if not snap.nodes and not snap.links:
        raise ValueError(f"{key} returned no devices and no links: there is nothing to mirror, so no snapshot was kept")
    now = time.time()
    links = [{k: v for k, v in l.items() if not k.startswith("_")} for l in snap.links]
    for l in links:
        l["rate_mbps"] = round(float(l["rate_mbps"]), 6)
        l["delay_ms"] = round(float(l["delay_ms"]), 6)
    c = snap.count
    measured = (c["link_rate_measured"] / c["links"]) if c["links"] else 0.0
    return {
        "snapshot_id": f"{key}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(now))}",
        "domain": "ip", "network": key, "controller": row.get("label") or key,
        "taken_at": now, "source": f"mirrored from {key} through the agentic MCP ({', '.join(sorted(set(reads.values())))})",
        "topology": {"nodes": [{"id": n["id"], "role": n["role"]} for n in snap.nodes.values()], "links": links},
        "traffic": {"demands": snap.demands},
        "aliases": {k: v for k, v in snap.aliases.items() if k != v},
        # Each link's ends as the controller names them: {link: [[device, port], [device, port]]}, so a
        # write that names a device's port finds its link.
        "ports": {l["id"]: [[l["_raw"][0], l["_ports"][0]], [l["_raw"][1], l["_ports"][1]]] for l in snap.links},
        "coverage": {**c, "share_of_link_rates_measured": round(measured, 3)},
        "provenance": snap.prov,
    }


def _counters(read, tools, v, devices) -> Dict[Tuple[str, str], Tuple[float, float]]:
    """{(device, port): (tx bytes, rx bytes)}."""
    need = src.required_args(tools, v["read_statistics"])
    out: Dict[Tuple[str, str], Tuple[float, float]] = {}
    targets = list(devices)[:MAX_PORT_READS] if need else [None]
    for dev in targets:
        got = read("read_statistics", {need[0]: dev} if need else {})
        _walk(got, dev, out)
    if not out:
        raise RuntimeError("the statistics read returned no byte counters")
    return out


def _walk(node: Any, dev: Optional[str], out: Dict) -> None:
    if isinstance(node, list):
        for x in node:
            _walk(x, dev, out)
        return
    if not isinstance(node, dict):
        return
    here = _scalar({k: node[k] for k in node if k in ("device", "dpid", "router", "switch", "deviceId")}) or dev
    tx, rx = _first(node, _TX_KEYS), _first(node, _RX_KEYS)
    port = _first(node, _PORT_KEYS)
    if here is not None and port is not None and (tx is not None or rx is not None):
        try:
            out[(str(here), str(port))] = (float(tx or 0), float(rx or 0))
        except (TypeError, ValueError):
            pass
        return
    for k, x in node.items():
        if isinstance(x, (list, dict)):
            _walk(x, here if here is not None else (k if isinstance(x, list) else None), out)


def _background(snap: _Snap, first, second, dt: float) -> None:
    """Per link and direction: the measured rate minus what the modelled services put there (on their
    shortest path, as the twin routes them); the rest is a one-hop background demand."""
    rate = {k: max(0.0, (second[k][0] - first[k][0]) * 8 / dt / 1e6) for k in second if k in first}
    service_load = _service_load(snap)
    for l in snap.links:
        (ra, rz), (pa, pz) = l["_raw"], l["_ports"]
        for (dev, port, s, d) in ((ra, pa, l["a"], l["z"]), (rz, pz, l["z"], l["a"])):
            if port is None or (str(dev), str(port)) not in rate:
                continue
            snap.count["links_with_traffic_measured"] += 0.5
            extra = rate[(str(dev), str(port))] - service_load.get((l["id"], s), 0.0)
            if extra >= MIN_BACKGROUND_MBPS:
                snap.demands.append({"id": safe_id(f"bg.{l['id']}.{s}")[:64], "src": s, "dst": d, "protocol": "udp",
                                     "rate_mbps": round(extra, 4), "start_s": 1.0})
                snap.count["background_demands"] += 1
    snap.count["links_with_traffic_measured"] = int(snap.count["links_with_traffic_measured"] + 0.5)
    snap.prov.append(f"traffic: counters read twice, {dt:.1f} s apart; {snap.count['background_demands']} "
                     f"link direction(s) carry traffic beyond the services, run as background")


def _service_load(snap: _Snap) -> Dict[Tuple[str, str], float]:
    adj: Dict[str, List[Tuple[str, str]]] = {}
    for l in snap.links:
        adj.setdefault(l["a"], []).append((l["z"], l["id"]))
        adj.setdefault(l["z"], []).append((l["a"], l["id"]))
    load: Dict[Tuple[str, str], float] = {}
    for d in snap.demands:
        prev = {d["src"]: None}
        q = deque([d["src"]])
        while q:
            n = q.popleft()
            for m, lid in adj.get(n, []):
                if m not in prev:
                    prev[m] = (n, lid)
                    q.append(m)
        n = d["dst"]
        while prev.get(n):
            p, lid = prev[n]
            load[(lid, p)] = load.get((lid, p), 0.0) + d["rate_mbps"]
            n = p
    return load


def trust_caps(snap: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    """The best trust tier a prediction on this snapshot may have, and why: a twin whose link capacities
    were mostly assumed cannot be trusted to let a change go ahead, however well calibrated."""
    c = snap.get("coverage") or {}
    if not c:
        return None, []
    share = c.get("share_of_link_rates_measured", 0.0)
    why = []
    cap = None
    if share < 0.5:
        cap = "C"
        why.append(f"only {share:.0%} of link capacities came from the controller; the rest are assumed")
    elif share < 1.0:
        cap = "B"
        why.append(f"{share:.0%} of link capacities came from the controller")
    if c.get("services") and not c.get("links_with_traffic_measured"):
        cap = cap or "B"
        why.append("no traffic counters: today's load is not in the twin")
    return cap, why


def save(snap: Dict[str, Any], folder) -> str:
    from pathlib import Path
    p = Path(folder) / f"{snap['snapshot_id']}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap, indent=1))
    return str(p)
