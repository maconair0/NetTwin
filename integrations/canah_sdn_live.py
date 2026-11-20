"""
The live loop, end to end: CANAH holds an SDN controller through the agentic MCP, netwins mirrors it
into a twin, every write is tried in the twin first and held at the gate unless the twin supports it or
the operator overrides it.

  CANAH_ROOT=~/CANAH  ~/CANAH/.venv/bin/python integrations/canah_sdn_live.py [--only id,id] [--engine docker|fake]
                                                                               [--calibration FILE]

The controller is netwins' SDN lab (netwins/testing/sdn_lab.py) on a free port. Every write the agent
proposes is approved, as an operator would: what is checked is that the twin, not the approval, decides
what reaches the network. Each case is judged on the lab's state afterwards and on the agent's calls.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List

HERE = Path(__file__).resolve().parent.parent
CANAH = Path(os.getenv("CANAH_ROOT", str(Path.home() / "CANAH"))).resolve()


@dataclass
class Case:
    id: str
    turns: List[str]
    check: Callable[[str, list, object, list], str]   # (last answer, calls, lab, twin checks) -> "" or why not
    reset: Callable[[object], None] = field(default=lambda lab: None)


def names(calls):
    return [c["name"] for c in calls]


def written(lab, tool, **match):
    return [w for w in lab.writes if w["tool"] == tool and all(w["arguments"].get(k) == v for k, v in match.items())]


def twin_said(checks, tool, action):
    return [c for c in checks if c["tool"] == tool and c["action"] == action]


def case_small(text, calls, lab, checks):
    if not written(lab, "add_path", name="web1"):
        return "web1 was not created"
    if not twin_said(checks, "add_path", "propose"):
        return "no twin check supported the write"
    return ""


def case_big(text, calls, lab, checks):
    if written(lab, "add_path"):
        return f"a rejected service reached the network: {lab.writes}"
    if not twin_said(checks, "add_path", "reject"):
        return "the twin never rejected it"
    if not re.search(r"reject|not (be )?(created|sent|safe)|would (exceed|overload|congest)|loss", text, re.I):
        return "the answer does not say the twin rejected it"
    return ""


def case_no_rate(text, calls, lab, checks):
    got = written(lab, "add_path", name="vid1")
    if not got:
        return "vid1 was not created after the operator gave its rate"
    if float(got[0]["arguments"].get("bandwidth_mbps") or 0) != 15:
        return f"vid1 was created with {got[0]['arguments']}, not the 15 Mbit/s the operator gave"
    return ""


def case_shut_ok(text, calls, lab, checks):
    if ["p-1", 3] not in lab.st["ports_down"]:
        return "p-1 port 3 was not shut"
    return ""


def case_shut_bad_then_override(text, calls, lab, checks):
    if ["p-1", 2] not in lab.st["ports_down"]:
        return "the operator's go-ahead did not get the port shut"
    if not [c for c in checks if c["tool"] == "set_port" and c.get("override")]:
        return "the port was shut without an override of the twin's rejection"
    return ""


def case_mirror(text, calls, lab, checks):
    if "twin_mirror" not in names(calls):
        return "the network was not mirrored"
    if not re.search(r"L-a1|backgr|unaccounted|5(\.0)? ?Mbit|4\.9", text, re.I):
        return "the answer does not mention the traffic no service accounts for"
    return ""


CASES = [
    Case("mirror", ["Mirror the lab network into its digital twin and tell me what the twin holds, including any "
                    "traffic that no service accounts for."], case_mirror),
    Case("create-small", ["Create a path service called web1 from pe-a to dc-1 at 10 Mbit/s on the lab controller."],
         case_small),
    Case("create-too-big", ["Create a path service called bulk1 from pe-a to pe-b at 60 Mbit/s on the lab "
                            "controller."], case_big),
    Case("create-no-rate", ["On the lab controller, set up a path service called vid1 from pe-a to pe-b.",
                            "It will carry 15 Mbit/s."], case_no_rate),
    Case("shut-port-ok", ["On the lab controller, shut port 3 of switch p-1 (admin state down)."], case_shut_ok),
    Case("shut-port-override", ["On the lab controller, take port 2 of switch p-1 down (admin state down) for "
                                "maintenance.",
                                "I understand the risk. Go ahead anyway: the customers have been told and this is a "
                                "planned maintenance window."], case_shut_bad_then_override),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--engine", default="docker")
    ap.add_argument("--calibration", default="")
    a = ap.parse_args(argv)

    home = Path(tempfile.mkdtemp(prefix="netwins-sdn-"))
    os.environ.update({"NETWINS_HOME": str(home), "NETWINS_SNAPSHOTS": str(home / "snapshots"),
                       "NETWINS_ENGINE": a.engine, "CANAH_HOME": str(home / "canah")})
    if a.calibration:
        import shutil
        shutil.copy(a.calibration, home / "calibration.sqlite")
    sys.path[:0] = [str(HERE), str(CANAH)]

    from canah import approval, build_agent, load_settings, models
    from canah.identity import Identity
    from canah.network import AgenticMCP, AgenticMCPProvider
    from netwins.testing.sdn_lab import serve

    srv, url, lab = serve()
    s = load_settings(path=str(CANAH / "canah.yaml"))
    s.providers = []
    s.home = str(home / "canah")
    s.sandbox = "none"
    s.skills_dirs = s.skills_dirs + [str(HERE / "skills")]
    os.environ.update({"AGENTIC_MCP_URL": url, "AGENTIC_MCP_TOKEN": "", "NETWINS_MIRROR_INTERVAL_S": "2"})
    s.twins = [{"name": "netwins", "kind": "python", "spec": "netwins.contract:twin", "path": str(HERE)}]
    s.twin_active = "netwins"
    agent = build_agent(s, [AgenticMCPProvider(service=AgenticMCP(url, token=""))])
    who = Identity("operator", "admin")
    print(f"model {models.describe()['model']} · {agent.info['tools']} tools · engine {a.engine} · "
          f"calibration {'given' if a.calibration else 'none'} · lab {url} · home {home}", flush=True)

    only = set(filter(None, a.only.split(",")))
    rows = []
    for c in [c for c in CASES if not only or c.id in only]:
        lab.reset()
        agent.twin_checks.by_id.clear()
        from netwins.contract import twin as netwins_twin
        if netwins_twin()._checker is not None:
            netwins_twin()._checker.checks.clear()
            netwins_twin()._checker.by_key.clear()
        for f in (home / "snapshots").glob("*.json"):
            f.unlink()
        thread = f"sdn-{int(time.time())}-{c.id}"
        t0 = time.time()
        calls, text, approved, err = [], "", [], ""
        try:
            for turn in c.turns:
                r = agent.ask(turn, thread_id=thread, who=who)
                for _ in range(6):
                    if not r.pending:
                        break
                    approved += [p["name"] for p in r.pending]
                    w = Identity(who.user, "root") if any(p.get("needs") == "root" for p in r.pending) else who
                    r = agent.decide(thread, [approval.decision("approve") for _ in r.pending], who=w)
                calls += r.tool_calls
                text = r.text or ""
        except Exception as e:  # noqa: BLE001 - a crash is a failed case
            err = f"{type(e).__name__}: {e}"
        checks = [{k: v.get(k) for k in ("tool", "arguments", "action", "override")}
                  for v in agent.twin_checks.by_id.values()]
        why = err or c.check(text, calls, lab, checks)
        rows.append({"case": c.id, "ok": not why, "why": why, "seconds": round(time.time() - t0, 1),
                     "calls": names(calls), "approved": approved, "lab_writes": lab.writes,
                     "refused_by_lab": lab.refused, "twin_checks": checks, "answer": text, "error": err})
        print(f"{'PASS' if not why else 'FAIL'} {c.id:20} {rows[-1]['seconds']:6.1f}s  approved={approved}  "
              f"checks={[(x['tool'], x['action']) for x in checks]}  lab writes={[w['tool'] for w in lab.writes]}"
              + (f"\n     why: {why}" if why else ""), flush=True)
        print("     calls:", names(calls), flush=True)
        print("     answer:", text[:500].replace("\n", " | "), flush=True)
    agent.close()
    srv.shutdown()
    out = HERE / "integrations" / "results"
    out.mkdir(exist_ok=True)
    f = out / f"canah-sdn-{time.strftime('%Y%m%d-%H%M%S')}.json"
    f.write_text(json.dumps({"model": models.describe()["model"], "engine": a.engine,
                             "calibration": bool(a.calibration), "runs": rows}, indent=1, default=str))
    print(f"\n{sum(r['ok'] for r in rows)}/{len(rows)} passed · {f}")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
