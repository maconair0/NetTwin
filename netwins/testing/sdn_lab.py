"""
A small SDN controller behind an agentic-MCP-compatible HTTP service, for netwins' own tests and demos.

It is not a twin: its traffic model is a fluid one (services on shortest paths, capped by the busiest
link), enough to make counters move the way a live network's do. Its tool names are its own, not the
contract's, and its `conformance` maps contract verbs to them, as a real agentic MCP's onboarding does,
so the mirror has to read that mapping to find its way.

  GET  /controllers  /controllers/lab/tools  /health
  POST /controllers/lab/call      {tool, arguments, approved_by} -> {text, is_error}
  POST /reset                     back to the start

A write without `approved_by` is refused, as the agentic MCP's own write gate does.

  python -m netwins.testing.sdn_lab [--port 8420]
"""
from __future__ import annotations

import argparse
import copy
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

START = {
    "switches": [{"dpid": d, "role": r} for d, r in
                 [("pe-a", "edge"), ("pe-b", "edge"), ("p-1", "core"), ("p-2", "core"), ("p-3", "core"),
                  ("dc-1", "edge")]],
    "wiring": [
        {"name": "L-a1", "src": {"dpid": "pe-a", "port_no": 1}, "dst": {"dpid": "p-1", "port_no": 1},
         "capacity_mbps": 100, "latency_ms": 2},
        {"name": "L-12", "src": {"dpid": "p-1", "port_no": 2}, "dst": {"dpid": "p-2", "port_no": 1},
         "capacity_mbps": 100, "latency_ms": 8},
        {"name": "L-2b", "src": {"dpid": "p-2", "port_no": 2}, "dst": {"dpid": "pe-b", "port_no": 1},
         "capacity_mbps": 100, "latency_ms": 2},
        {"name": "L-13", "src": {"dpid": "p-1", "port_no": 3}, "dst": {"dpid": "p-3", "port_no": 1},
         "capacity_mbps": 40, "latency_ms": 12},
        {"name": "L-3b", "src": {"dpid": "p-3", "port_no": 2}, "dst": {"dpid": "pe-b", "port_no": 2},
         "capacity_mbps": 40, "latency_ms": 12},
        {"name": "L-bd", "src": {"dpid": "pe-b", "port_no": 3}, "dst": {"dpid": "dc-1", "port_no": 1},
         "capacity_mbps": 100, "latency_ms": 1},
    ],
    "services": {"acme": {"from": "pe-a", "to": "pe-b", "bandwidth_mbps": 30},
                 "globex": {"from": "pe-a", "to": "dc-1", "bandwidth_mbps": 20}},
    # Traffic no service accounts for (a backup job, say): on L-a1, pe-a to p-1.
    "unaccounted": [{"link": "L-a1", "from": "pe-a", "mbps": 5.0}],
    "ports_down": [],
}

CONFORMANCE = {"verdict": "accepted", "domains": [
    {"domain": "inventory", "status": "covered", "features": {"read_devices": "tool get_switches"}},
    {"domain": "topology", "status": "covered", "features": {"read_links": "tool get_wiring"}},
    {"domain": "services", "status": "covered", "features": {"read_services": "tool get_paths"}},
    {"domain": "resources", "status": "covered", "features": {"read_statistics": "tool port_counters"}},
    {"domain": "provisioning", "status": "covered",
     "features": {"provision": "tool add_path", "teardown": "tool drop_path"}},
    {"domain": "adjustment", "status": "partly", "features": {"adjust": "tool resize_path", "reroute": "missing"}},
    {"domain": "device configuration", "status": "covered", "features": {"configure_device": "tool set_port"}},
]}

_S = {"type": "string"}
_N = {"type": "number"}
TOOLS = [
    {"name": "get_switches", "description": "The switches, with their role.", "write": False,
     "schema": {"type": "object", "properties": {}}},
    {"name": "get_wiring", "description": "Links between switches: ends (dpid, port_no), capacity, latency.",
     "write": False, "schema": {"type": "object", "properties": {}}},
    {"name": "get_paths", "description": "The path services: ends and bandwidth.", "write": False,
     "schema": {"type": "object", "properties": {}}},
    {"name": "port_counters", "description": "Byte counters of every switch port.", "write": False,
     "schema": {"type": "object", "properties": {}}},
    {"name": "add_path", "description": "Create a path service between two switches at a bandwidth.", "write": True,
     "schema": {"type": "object", "properties": {"name": _S, "from": _S, "to": _S, "bandwidth_mbps": _N},
                "required": ["name", "from", "to"]}},
    {"name": "drop_path", "description": "Remove a path service.", "write": True,
     "schema": {"type": "object", "properties": {"name": _S}, "required": ["name"]}},
    {"name": "resize_path", "description": "Change a path service's bandwidth.", "write": True,
     "schema": {"type": "object", "properties": {"name": _S, "bandwidth_mbps": _N},
                "required": ["name", "bandwidth_mbps"]}},
    {"name": "set_port", "description": "Set a switch port's admin state (up or down).", "write": True,
     "schema": {"type": "object", "properties": {"dpid": _S, "port_no": {"type": "integer"}, "admin_state": _S},
                "required": ["dpid", "port_no", "admin_state"]}},
]


class Lab:
    def __init__(self):
        self.reset()

    def reset(self):
        self.st = copy.deepcopy(START)
        self.t0 = time.monotonic()
        self.bytes: Dict[tuple, float] = {}
        self.last = self.t0
        self.writes: List[Dict[str, Any]] = []
        self.refused: List[str] = []
        self.lock = threading.Lock()

    # -- traffic
    def _up_links(self):
        down = {tuple(p) for p in self.st["ports_down"]}
        return [l for l in self.st["wiring"]
                if (l["src"]["dpid"], l["src"]["port_no"]) not in down and (l["dst"]["dpid"], l["dst"]["port_no"]) not in down]

    def _path(self, a, z, links):
        adj = {}
        for l in links:
            adj.setdefault(l["src"]["dpid"], []).append((l["dst"]["dpid"], l, "fwd"))
            adj.setdefault(l["dst"]["dpid"], []).append((l["src"]["dpid"], l, "rev"))
        prev = {a: None}
        q = deque([a])
        while q:
            n = q.popleft()
            for m, l, d in adj.get(n, []):
                if m not in prev:
                    prev[m] = (n, l, d)
                    q.append(m)
        if z not in prev:
            return None
        out, n = [], z
        while prev[n]:
            p, l, d = prev[n]
            out.append((l, d))
            n = p
        return list(reversed(out))

    def _rates(self) -> Dict[tuple, float]:
        """Mbit/s leaving each (dpid, port) now."""
        links = self._up_links()
        offered = []
        for s in self.st["services"].values():
            p = self._path(s["from"], s["to"], links)
            if p:
                offered.append((float(s["bandwidth_mbps"]), p))
        for u in self.st["unaccounted"]:
            l = next((l for l in links if l["name"] == u["link"]), None)
            if l:
                offered.append((u["mbps"], [(l, "fwd" if l["src"]["dpid"] == u["from"] else "rev")]))
        load: Dict[tuple, float] = {}
        for r, p in offered:
            for l, d in p:
                load[(l["name"], d)] = load.get((l["name"], d), 0) + r
        out: Dict[tuple, float] = {}
        for r, p in offered:
            share = min([1.0] + [l["capacity_mbps"] / load[(l["name"], d)] for l, d in p])
            for l, d in p:
                end = l["src"] if d == "fwd" else l["dst"]
                key = (end["dpid"], end["port_no"])
                out[key] = out.get(key, 0) + r * share
        return out

    def _advance(self):
        now = time.monotonic()
        dt = now - self.last
        for k, mbps in self._rates().items():
            self.bytes[k] = self.bytes.get(k, 0) + mbps * 1e6 / 8 * dt
        self.last = now

    # -- tools
    def call(self, tool: str, a: Dict[str, Any], approved_by: str) -> Dict[str, Any]:
        with self.lock:
            self._advance()
            spec = next((t for t in TOOLS if t["name"] == tool), None)
            if spec is None:
                return {"text": f"Error: no tool {tool}", "is_error": True}
            if spec["write"] and not approved_by:
                self.refused.append(tool)
                return {"text": "Error: refused by the write gate: no approval", "is_error": True}
            try:
                text = getattr(self, "t_" + tool)(**a)
            except TypeError as e:
                return {"text": f"Error: {e}", "is_error": True}
            if spec["write"] and not text.startswith("Error"):
                self.writes.append({"tool": tool, "arguments": a, "by": approved_by})
            return {"text": text, "is_error": text.startswith("Error")}

    def t_get_switches(self):
        return json.dumps({"switches": self.st["switches"]})

    def t_get_wiring(self):
        down = {tuple(p) for p in self.st["ports_down"]}
        rows = []
        for l in self.st["wiring"]:
            st = "down" if ((l["src"]["dpid"], l["src"]["port_no"]) in down or
                            (l["dst"]["dpid"], l["dst"]["port_no"]) in down) else "up"
            rows.append({**l, "state": st})
        return json.dumps({"links": rows})

    def t_get_paths(self):
        return json.dumps({"services": [{"name": k, **v} for k, v in self.st["services"].items()]})

    def t_port_counters(self):
        per: Dict[str, List[Dict[str, Any]]] = {}
        for sw in self.st["switches"]:
            per[sw["dpid"]] = []
        for l in self.st["wiring"]:
            for end in (l["src"], l["dst"]):
                k = (end["dpid"], end["port_no"])
                per[end["dpid"]].append({"port_no": end["port_no"], "tx_bytes": int(self.bytes.get(k, 0)),
                                         "rx_bytes": 0})
        return json.dumps(per)

    def _sw(self, d):
        return any(s["dpid"] == d for s in self.st["switches"])

    def t_add_path(self, name, to, bandwidth_mbps=None, **kw):
        src = kw.get("from")
        if name in self.st["services"]:
            return f"Error: service {name} exists"
        if not (self._sw(src) and self._sw(to)):
            return f"Error: unknown switch {src} or {to}"
        if bandwidth_mbps is None:
            return "Error: bandwidth_mbps is needed"
        self.st["services"][name] = {"from": src, "to": to, "bandwidth_mbps": float(bandwidth_mbps)}
        return f"service {name} created: {src} to {to} at {bandwidth_mbps} Mbit/s"

    def t_drop_path(self, name):
        if name not in self.st["services"]:
            return f"Error: no service {name}"
        del self.st["services"][name]
        return f"service {name} removed"

    def t_resize_path(self, name, bandwidth_mbps):
        if name not in self.st["services"]:
            return f"Error: no service {name}"
        self.st["services"][name]["bandwidth_mbps"] = float(bandwidth_mbps)
        return f"service {name} now {bandwidth_mbps} Mbit/s"

    def t_set_port(self, dpid, port_no, admin_state):
        key = [dpid, int(port_no)]
        if admin_state.lower() in ("down", "disable", "disabled"):
            if key not in self.st["ports_down"]:
                self.st["ports_down"].append(key)
        else:
            self.st["ports_down"] = [p for p in self.st["ports_down"] if p != key]
        return f"{dpid} port {port_no} is {admin_state}"


def serve(port: int = 0, lab: Optional[Lab] = None):
    """Start the lab in a thread. Returns (server, url, lab)."""
    lab = lab or Lab()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if self.path == "/controllers":
                return self._send(200, {"controllers": [{"key": "lab", "label": "SDN lab", "domain": "packet",
                                                         "family": "lab", "tools": len(TOOLS),
                                                         "conformance": CONFORMANCE}]})
            if self.path == "/controllers/lab/tools":
                return self._send(200, {"tools": TOOLS})
            if self.path == "/sessions":
                return self._send(200, {"sessions": [{"key": "lab", "ok": True}]})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/reset":
                lab.reset()
                return self._send(200, {"ok": True})
            if self.path == "/controllers/lab/call":
                return self._send(200, lab.call(body.get("tool", ""), body.get("arguments") or {},
                                                body.get("approved_by") or ""))
            return self._send(404, {"error": "not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", lab


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8420)
    a = ap.parse_args(argv)
    srv, url, _ = serve(a.port)
    print(f"SDN lab at {url} (controller key: lab)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
