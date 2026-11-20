"""
netwins in CANAH across its twins, live: a CANAH agent with the netwins provider and skills, asked
what-ifs about an IP, an optical and a Wi-Fi network in plain words, on the real engines (ns-3 in Docker,
GNPy). Each case checks that the right twin ran and that the answer reports the twin's own decision.

  CANAH_ROOT=~/CANAH ~/CANAH/.venv/bin/python integrations/canah_domains_live.py [--only id,id]
                                                [--calibration FILE] [--optical-lab]

--optical-lab first fills the calibration store from the lab's physics (optical_lab.calibrate).
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
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CANAH = Path(os.getenv("CANAH_ROOT", str(Path.home() / "CANAH"))).resolve()

WORDS = {"reject": r"\breject", "gather_more_data": r"gather[_ ]more[_ ]data|more data|not (yet )?(clear|certain|decided)|undecided",
         "propose": r"\bpropos", "met": r"\bmet\b|\bmeets?\b|holds", "not_met": r"not[_ ]met|not meet|does not hold"}


def predicts(calls, domain=None):
    out = []
    for c in calls:
        if c["name"] != "twin_predict" or not c.get("result"):
            continue
        sc = (c.get("args") or {}).get("scenario")
        if isinstance(sc, str):
            try:
                sc = json.loads(sc)
            except ValueError:
                sc = {}
        if domain is None or (sc or {}).get("domain", "ip") == domain:
            try:
                out.append((sc, json.loads(c["result"])))
            except ValueError:
                pass
    return out


def expect(domain, decision=None, also=""):
    def check(text, calls):
        got = predicts(calls, domain)
        if not got:
            return f"no {domain} prediction was run"
        res = got[-1][1]
        action = (res.get("decision") or {}).get("action")
        if decision and action not in decision:
            return f"the {domain} twin decided {action}, expected {decision} (the scenario was built differently)"
        if not re.search(WORDS.get(action, re.escape(str(action))), text, re.I):
            return f"the answer does not report the twin's decision ({action})"
        if also and not re.search(also, text, re.I):
            return f"the answer does not mention /{also}/"
        return ""
    return check


CASES = [
    ("twins", "Which digital twins do you have, and what does each one not model?",
     lambda text, calls: "" if re.search(r"optical", text, re.I) and re.search(r"wi-?fi", text, re.I)
     else "the answer does not name the optical and Wi-Fi twins"),
    ("optical-cut", "An optical network has three ROADM sites A, B and C, with fibres A-B of 240 km, A-C of 400 km "
     "and C-B of 380 km. Lightpath LP-1 is 200G from A to B over the A-B fibre, set to restore, and carries "
     "40 Gbit/s for acme. If the A-B fibre is cut, does LP-1 keep at least 3 dB of margin? Fibre loss is "
     "0.21 dB/km, give or take 0.01.",
     expect("optical", also=r"margin|dB")),
    ("optical-400g", "Same optical network as before: would LP-1 still keep 3 dB of margin after the A-B cut if it "
     "ran at 400G instead of 200G?", expect("optical", decision=("reject",), also=r"margin|dB")),
    ("wifi-ap-fail", "A Wi-Fi network has two access points on channel 36: ap-1 at (0, 0) and ap-2 at (30, 0), in "
     "metres. A camera at (8, 3) sends 8 Mbit/s to the server and a laptop at (25, -4) downloads 20 Mbit/s from "
     "it. If ap-1 fails 5 seconds in, does the camera keep its loss under 1% over a 12 second run?",
     expect("wifi", decision=("reject", "gather_more_data"), also=r"loss|camera")),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--calibration", default="")
    ap.add_argument("--optical-lab", action="store_true")
    a = ap.parse_args(argv)
    home = Path(tempfile.mkdtemp(prefix="netwins-domains-"))
    if a.calibration:
        shutil.copy(a.calibration, home / "calibration.sqlite")
    os.environ.update({"NETWINS_HOME": str(home), "NETWINS_ENGINE": "docker", "CANAH_HOME": str(home / "canah")})
    sys.path[:0] = [str(HERE), str(CANAH)]
    from canah import build_agent, load_settings, models
    from canah.identity import Identity
    from netwins.contract import twin as netwins_twin

    if a.optical_lab:
        from netwins import optical_lab
        got = optical_lab.calibrate(netwins_twin().twin.calibration)
        print("optical calibration from the lab:", {k: (v["n"], v["trust_tier"]) for k, v in got["status"].items()}, flush=True)
    s = load_settings(path=str(CANAH / "canah.yaml"))
    s.providers = []
    s.home = str(home / "canah")
    s.sandbox = "none"
    s.skills_dirs = s.skills_dirs + [str(HERE / "skills")]
    s.twins = [{"name": "netwins", "kind": "python", "spec": "netwins.contract:twin", "path": str(HERE)}]
    s.twin_active = "netwins"
    agent = build_agent(s, [])
    who = Identity("operator", "admin")
    print(f"model {models.describe()['model']} · {agent.info['tools']} tools · home {home}", flush=True)
    only = set(filter(None, a.only.split(",")))
    thread = f"dom-{int(time.time())}"
    rows = []
    for cid, question, check in [c for c in CASES if not only or c[0] in only]:
        t0 = time.time()
        try:
            r = agent.ask(question, thread_id=thread if cid.startswith("optical") else f"{thread}-{cid}", who=who)
            text, calls, err = r.text or "", r.tool_calls, ""
            why = check(text, calls)
        except Exception as e:  # noqa: BLE001
            text, calls, err, why = "", [], f"{type(e).__name__}: {e}", "crash"
        pred = [(sc.get("domain", "ip"), (res.get("decision") or {}).get("action"), (res.get("trust") or {}).get("tier"),
                 (res.get("probability") or {}).get("p")) for sc, res in predicts(calls)]
        rows.append({"case": cid, "ok": not why, "why": why, "seconds": round(time.time() - t0, 1),
                     "calls": [c["name"] for c in calls], "predictions": pred, "answer": text, "error": err,
                     "trace": [{"name": c["name"], "args": c.get("args"), "result": (c.get("result") or "")[:1500]} for c in calls]})
        print(f"{'PASS' if not why else 'FAIL'} {cid:14} {rows[-1]['seconds']:6.1f}s  predictions={pred}"
              + (f"\n     why: {why}" if why else ""), flush=True)
        print("     answer:", text[:600].replace("\n", " | "), flush=True)
    agent.close()
    out = HERE / "integrations" / "results"
    out.mkdir(exist_ok=True)
    f = out / f"canah-domains-{time.strftime('%Y%m%d-%H%M%S')}.json"
    f.write_text(json.dumps({"model": models.describe()["model"], "runs": rows}, indent=1, default=str))
    print(f"\n{sum(r['ok'] for r in rows)}/{len(rows)} passed · {f}")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
