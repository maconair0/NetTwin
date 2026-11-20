"""
The ensemble: a scenario expanded into members, each run once by an engine.

Member i gets seed base_seed+i and one sample of every uncertain parameter, drawn from a generator
seeded by i, so the baseline and the changed scenario of a pair see the same inputs (common random
numbers): their difference is the change's effect, not noise.

Engines (all `run(member: dict, seed: int) -> dict`):
  DockerEngine   one container per member: the netwins-ns3 image, no network, read-only root,
                 CPU/memory/wall-clock limits, non-root, scenario mounted read-only
  LocalEngine    a netwins-ip binary on this machine (development)
  FakeEngine     a fast stand-in with a simple, deterministic model (tests only; never a twin)
"""
from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .schema import Scenario

IMAGE = os.getenv("NETWINS_IMAGE", "netwins-ns3:3.43-r2")
ENGINE_VERSION = {"ns3": "3.43", "image": IMAGE, "schema": "1.0"}


def sample(sc: Scenario, i: int) -> Dict[str, float]:
    """One draw of every uncertain parameter, for member i."""
    rng = np.random.default_rng([sc.seed_policy.base_seed, i])
    out: Dict[str, float] = {}
    for u in sc.uncertain_params:
        if u.dist == "normal":
            v = rng.normal(u.mean, u.std)
        elif u.dist == "lognormal":
            # mean/std of the value itself, turned into the underlying normal's parameters
            var = math.log(1 + (u.std / u.mean) ** 2) if u.mean else 0.0
            v = rng.lognormal(math.log(u.mean) - var / 2, math.sqrt(var)) if u.mean else 0.0
        else:
            v = rng.uniform(u.low, u.high)
        out[u.path] = float(v)
    return out


def members(sc: Any, start: int, count: int) -> List[Tuple[int, Dict[str, Any], Dict[str, Any]]]:
    """[(seed, baseline member, changed member)] for members start..start+count-1, of any domain."""
    from . import domains
    dom = domains.get(sc.domain)
    base, changed = dom.baseline_and_changed(sc)
    out = []
    for i in range(start, start + count):
        draws = sample(sc, i)
        b, c = copy.deepcopy(base), copy.deepcopy(changed)
        for path, v in draws.items():
            for m in (b, c):
                try:
                    dom.set_path(m, path, dom.clamp(path, v))
                except ValueError:
                    pass          # the change removed what this path points at: nothing to sample there
        out.append((sc.seed_policy.base_seed + i, b, c))
    return out


class DockerEngine:
    name = "docker"

    def __init__(self, image: str = IMAGE, context: str = "", cpus: str = "1", memory: str = "512m",
                 timeout_s: float = 300.0, program: str = "netwins-ip", view: Optional[Callable] = None):
        self.image, self.cpus, self.memory, self.timeout_s = image, cpus, memory, timeout_s
        self.context = context or os.getenv("NETWINS_DOCKER_CONTEXT", "default")
        self.program, self.view = program, view or _engine_view
        self.name = "docker" if program == "netwins-ip" else f"docker:{program}"

    def run(self, member: Dict[str, Any], seed: int) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="netwins-") as d:
            os.chmod(d, 0o777)
            with open(os.path.join(d, "member.json"), "w") as fh:
                json.dump(self.view(member), fh)
            cmd = ["docker", "--context", self.context, "run", "--rm", "--network", "none", "--read-only",
                   "--cpus", self.cpus, "--memory", self.memory, "--pids-limit", "64", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "-v", f"{d}:/work", "--entrypoint", self.program, self.image,
                   "--scenario=/work/member.json", "--out=/work/result.json", f"--run={seed}"]
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s)
            if done.returncode != 0:
                raise RuntimeError(f"engine failed (seed {seed}): {(done.stderr or done.stdout).strip()[:400]}")
            with open(os.path.join(d, "result.json")) as fh:
                return json.load(fh)


class LocalEngine:
    name = "local"

    def __init__(self, binary: str, timeout_s: float = 300.0, view: Optional[Callable] = None):
        self.binary, self.timeout_s = binary, timeout_s
        self.view = view or _engine_view

    def run(self, member: Dict[str, Any], seed: int) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="netwins-") as d:
            with open(os.path.join(d, "member.json"), "w") as fh:
                json.dump(self.view(member), fh)
            done = subprocess.run([self.binary, f"--scenario={d}/member.json", f"--out={d}/result.json",
                                   f"--run={seed}"], capture_output=True, text=True, timeout=self.timeout_s)
            if done.returncode != 0:
                raise RuntimeError(f"engine failed (seed {seed}): {(done.stderr or done.stdout).strip()[:400]}")
            with open(os.path.join(d, "result.json")) as fh:
                return json.load(fh)


class FakeEngine:
    """For tests only: a fluid model. Demands take a shortest surviving path and share each link in
    proportion to their rates; loss is what does not fit; link failures split the run in time. Not a twin."""
    name = "fake"

    def __init__(self, noise: float = 0.02):
        self.noise = noise
        self.calls = 0

    def run(self, member: Dict[str, Any], seed: int) -> Dict[str, Any]:
        """Demands share each link in proportion (a fluid model). A link_down event splits the run into
        before and after; each KPI is the time-weighted mix of the two states."""
        self.calls += 1
        rng = np.random.default_rng(seed)
        demands = member["traffic"]["demands"]
        downs = sorted((e["t"], e["target"]) for e in member.get("events", []) if e["type"] == "link_down")
        dur = float(member.get("duration_s", 30))
        phases, t0, down = [], 0.0, set()
        for t, target in downs:
            phases.append((t - t0, set(down)))
            down.add(target)
            t0 = t
        phases.append((dur - t0, set(down)))
        mix: Dict[str, Dict[str, float]] = {d["id"]: {"share": 0.0, "delay": 0.0, "lost": 0.0, "w": 0.0}
                                            for d in demands}
        for weight, gone in phases:
            if weight <= 0:
                continue
            links = [l for l in member["topology"]["links"] if l["id"] not in gone]
            paths = {d["id"]: _shortest(links, d["src"], d["dst"]) for d in demands}
            load: Dict[str, float] = {}
            for d in demands:
                for l in paths[d["id"]] or []:
                    load[l["id"]] = load.get(l["id"], 0.0) + d["rate_mbps"]
            for d in demands:
                m, path = mix[d["id"]], paths[d["id"]]
                m["w"] += weight
                if path is None:
                    m["lost"] += weight
                    continue
                share = min([1.0] + [l["rate_mbps"] / load[l["id"]] for l in path])
                m["share"] += weight * share
                m["delay"] += weight * sum(l.get("delay_ms", 1) for l in path)
        out = {"run": seed, "demands": {}}
        for d in demands:
            m = mix[d["id"]]
            w = m["w"] or 1.0
            share = m["share"] / w
            if share == 0:
                out["demands"][d["id"]] = {"throughput_mbps": 0.0, "delay_ms_mean": None, "jitter_ms_mean": None,
                                           "loss_ratio": 1.0, "tx_packets": 1000, "rx_packets": 0}
                continue
            thr = d["rate_mbps"] * share * (1 + rng.normal(0, self.noise))
            loss = (1 - share) * (1 + rng.normal(0, self.noise)) + abs(rng.normal(0, self.noise * 0.05))
            loss = min(max(loss, 0.0), 1.0)
            delay = m["delay"] / max(w - m["lost"], 1e-9) * (1 + abs(rng.normal(0, self.noise)))
            out["demands"][d["id"]] = {"throughput_mbps": max(thr, 0.0), "delay_ms_mean": delay,
                                       "jitter_ms_mean": delay * 0.05, "loss_ratio": loss,
                                       "tx_packets": 1000, "rx_packets": int(1000 * (1 - loss))}
        return out


def _shortest(links, src, dst):
    import heapq
    adj: Dict[str, list] = {}
    for l in links:
        adj.setdefault(l["a"], []).append((l["z"], l))
        adj.setdefault(l["z"], []).append((l["a"], l))
    best, heap = {src: (0, [])}, [(0, src, [])]
    while heap:
        cost, node, path = heapq.heappop(heap)
        if node == dst:
            return path
        for nxt, l in adj.get(node, []):
            if nxt not in best or best[nxt][0] > cost + 1:
                best[nxt] = (cost + 1, path + [l])
                heapq.heappush(heap, (cost + 1, nxt, path + [l]))
    return None


def _engine_view(member: Dict[str, Any]) -> Dict[str, Any]:
    """What the engine reads: nodes, links, demands, events, duration (the rest is the pipeline's)."""
    # An unset optional field leaves the engine to its default: it must not arrive as null (the engine
    # reads numbers, and a null is not one).
    def clean(items):
        return [{k: v for k, v in x.items() if v is not None} for x in items]
    return {"nodes": clean(member["topology"]["nodes"]), "links": clean(member["topology"]["links"]),
            "demands": clean(member["traffic"]["demands"]), "events": clean(member.get("events", [])),
            "duration_s": member["duration_s"]}


def run_batch(engine: Any, batch: List[Tuple[int, Dict[str, Any], Dict[str, Any]]], paired: bool,
              workers: int = 0) -> List[Dict[str, Any]]:
    """Run a batch of members (both halves of each pair when `paired`), in parallel."""
    workers = workers or max(1, min(len(batch) * (2 if paired else 1), (os.cpu_count() or 4) - 2))
    jobs: List[Callable[[], Dict[str, Any]]] = []
    for seed, base, changed in batch:
        jobs.append(lambda s=seed, m=changed: {"seed": s, "side": "changed", "result": engine.run(m, s)})
        if paired:
            jobs.append(lambda s=seed, m=base: {"seed": s, "side": "baseline", "result": engine.run(m, s)})
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        out = list(pool.map(lambda j: j(), jobs))
    for o in out:
        o["seconds"] = round(time.time() - t0, 2)
    return out
