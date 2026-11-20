"""
netwins inside CANAH, live: a CANAH agent with only the netwins provider and skill, asked checked
what-if questions. netwins stays a separate project: CANAH is found at CANAH_ROOT and run with its own
Python, nothing is copied either way.

  CANAH_ROOT=~/CANAH  ~/CANAH/.venv/bin/python integrations/canah_live.py [--only id,id] [--engine docker|fake]

Each case is checked on the answer and on the tool calls: the agent must have run twin_predict, and the
decision it reports must be the one the twin's policy returned (it never makes its own). Runs use a fresh
NETWINS_HOME with a copy of the snapshots, stamped now (a stale snapshot is rejected by policy, by design),
and, for the calibrated cases, the synthetic calibration made beforehand by `calibrate-synthetic`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

HERE = Path(__file__).resolve().parent.parent
CANAH = Path(os.getenv("CANAH_ROOT", str(Path.home() / "CANAH"))).resolve()


@dataclass
class Case:
    id: str
    question: str
    check: Callable[[str, list], str]      # returns "" when it passes, else why not


def predicts(calls):
    return [c for c in calls if c["name"] == "twin_predict" and c.get("result")]


def last_decision(calls):
    for c in reversed(predicts(calls)):
        try:
            return json.loads(c["result"]).get("decision", {}).get("action")
        except ValueError:
            continue
    return None


def reports(action):
    words = {"reject": r"\breject", "gather_more_data": r"gather[_ ]more[_ ]data|more data",
             "propose": r"\bpropos", "auto_apply_eligible": r"auto[_ ]apply",
             "met": r"\bmet\b|\bmeets?\b|holds|below 1|under 1", "not_met": r"not[_ ]met|not meet|does not hold|exceed"}
    return re.compile(words[action], re.I)


def expect(action, *, also: str = ""):
    def check(text, calls):
        got = last_decision(calls)
        if got is None:
            return "no twin_predict result"
        if got != action:
            return f"the twin decided {got}, expected {action} (the scenario was built wrongly)"
        if not reports(action).search(text):
            return f"the answer does not report the twin's decision ({action})"
        if also and not re.search(also, text, re.I):
            return f"the answer does not mention /{also}/"
        return ""
    return check


def no_predict_needed(pattern):
    def check(text, calls):
        return "" if re.search(pattern, text, re.I) else f"the answer does not mention /{pattern}/"
    return check


CASES: List[Case] = [
    Case("domains", "Which networks have a digital twin, and what is in the IP one?",
         no_predict_needed(r"ip-lab-001|pe-a|L-12")),
    Case("l12-acme", "If link L-12 fails, does the acme service keep its packet loss under 1%?",
         expect("reject", also=r"tier|trust")),
    Case("l12-globex-delay", "Would globex's mean delay stay under 20 ms if L-12 went down 10 s into the run?",
         expect("reject")),
    Case("upgrade-detour", "We want to upgrade L-13 and L-3b to 100 Mbit/s. With that done, would acme still keep "
         "its loss under 1% if L-12 failed? Should we do it?", expect("propose", also=r"approv|human|operator")),
    Case("no-failure", "Today, with nothing failed, is acme's loss below 1%?", expect("met")),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--engine", default="docker")
    ap.add_argument("--calibration", default="", help="a calibration.sqlite to start from (else none: tier C)")
    a = ap.parse_args(argv)

    home = Path(tempfile.mkdtemp(prefix="netwins-canah-"))
    snaps = home / "snapshots"
    shutil.copytree(HERE / "data" / "snapshots", snaps)
    now = time.time()
    for p in snaps.glob("*.json"):
        s = json.loads(p.read_text())
        s["taken_at"] = now
        p.write_text(json.dumps(s))
    if a.calibration:
        shutil.copy(a.calibration, home / "calibration.sqlite")
    os.environ.update({"NETWINS_HOME": str(home), "NETWINS_SNAPSHOTS": str(snaps), "NETWINS_ENGINE": a.engine,
                       "CANAH_HOME": str(home / "canah")})
    sys.path[:0] = [str(HERE), str(CANAH)]

    from canah import build_agent, load_settings, models
    from canah.identity import Identity

    s = load_settings(path=str(CANAH / "canah.yaml"))
    s.providers = []
    s.home = str(home / "canah")
    s.sandbox = "none"
    s.skills_dirs = s.skills_dirs + [str(HERE / "skills")]
    s.twins = [{"name": "netwins", "kind": "python", "spec": "netwins.contract:twin", "path": str(HERE)}]
    s.twin_active = "netwins"
    agent = build_agent(s, [])
    who = Identity("operator", "admin")
    print(f"model {models.describe()['model']} · {agent.info['tools']} tools · engine {a.engine} · "
          f"calibration {'given' if a.calibration else 'none'} · home {home}", flush=True)

    only = set(filter(None, a.only.split(",")))
    rows = []
    for c in [c for c in CASES if not only or c.id in only]:
        t0 = time.time()
        try:
            r = agent.ask(c.question, thread_id=f"nw-{int(t0)}-{c.id}", who=who)
            text, calls = r.text or "", r.tool_calls
            why = c.check(text, calls)
            err = ""
        except Exception as e:  # noqa: BLE001 - a crash is a failed case
            text, calls, why, err = "", [], f"{type(e).__name__}: {e}", "crash"
        names = [x["name"] for x in calls]
        rows.append({"case": c.id, "ok": not why, "why": why, "seconds": round(time.time() - t0, 1),
                     "calls": names, "trace": [{"name": x["name"], "args": x.get("args"),
                                                      "result": (x.get("result") or "")[:1500]} for x in calls],
                     "decision": last_decision(calls), "answer": text, "error": err})
        print(f"{'PASS' if not why else 'FAIL'} {c.id:18} {rows[-1]['seconds']:6.1f}s  "
              f"decision={rows[-1]['decision']}  calls={names}" + (f"\n     why: {why}" if why else ""), flush=True)
        print("     answer:", text[:400].replace("\n", " | "), flush=True)
    agent.close()
    out = HERE / "integrations" / "results"
    out.mkdir(exist_ok=True)
    f = out / f"canah-live-{time.strftime('%Y%m%d-%H%M%S')}.json"
    f.write_text(json.dumps({"model": models.describe()["model"], "engine": a.engine,
                             "calibration": bool(a.calibration), "runs": rows}, indent=1))
    print(f"\n{sum(r['ok'] for r in rows)}/{len(rows)} passed · {f}")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
