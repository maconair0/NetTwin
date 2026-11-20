"""
CANAH with the twin, on the live controllers the agentic MCP holds (ONOS, Ryu, TeraFlowSDN, TransportPCE).
Nothing changes on the network: every write the agent proposes is REJECTED at CANAH's approval, so what is
tested is the reading, the mirroring, the twin's checks and the gate, never a change.

  CANAH_ROOT=~/CANAH ~/CANAH/.venv/bin/python integrations/canah_live_controllers.py [--only id,id] [--engine docker]

Needs AGENTIC_MCP_TOKEN in CANAH's .env (or the shell): the agentic MCP lists controllers without it, but
every call needs it. Results: integrations/results/canah-live-ctl-*.json, with the twin's checks per case.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CANAH = Path(os.getenv("CANAH_ROOT", str(Path.home() / "CANAH"))).resolve()

CASES = [
    ("inventory", ["Which controllers are there, which of them answer, and which have a digital twin?"]),
    ("mirror-onos", ["Mirror the ONOS network into its digital twin. How many devices, links and hosts does it hold, "
                     "and what did the twin have to assume?"]),
    ("mirror-ryu", ["Mirror the Ryu network into its digital twin. How many switches, links and hosts, and what was "
                    "assumed?"]),
    ("onos-change", ["On ONOS, I want a new 50 Mbit/s connection between the first two hosts. Check it in the twin "
                     "and propose it; do not send anything I have not approved."]),
    ("ryu-change", ["On Ryu, check in the twin whether shutting the first inter-switch link's port would hurt any "
                    "traffic, and propose it only if the twin supports it."]),
    ("optical-read", ["How many nodes and links does TransportPCE's topology have, and does the optical side have a "
                      "digital twin?"]),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--engine", default="docker")
    ap.add_argument("--calibration", default="")
    a = ap.parse_args(argv)
    home = Path(tempfile.mkdtemp(prefix="netwins-livectl-"))
    os.environ.update({"NETWINS_HOME": str(home), "NETWINS_SNAPSHOTS": str(home / "snapshots"),
                       "NETWINS_ENGINE": a.engine, "CANAH_HOME": str(home / "canah")})
    if a.calibration:
        import shutil
        shutil.copy(a.calibration, home / "calibration.sqlite")
    sys.path[:0] = [str(HERE), str(CANAH)]

    from canah import approval, build_agent, load_settings, models   # loads CANAH's .env
    from canah.identity import Identity
    from canah.network import AgenticMCPProvider
    from netwins.canah_provider import NetwinsProvider
    from netwins.gate import TwinGate

    if not os.getenv("AGENTIC_MCP_TOKEN"):
        print("AGENTIC_MCP_TOKEN is not set (CANAH's .env or the shell): the controllers cannot be called.")
        return 2
    s = load_settings(path=str(CANAH / "canah.yaml"))
    s.providers = []
    s.home = str(home / "canah")
    s.sandbox = "none"
    s.skills_dirs = s.skills_dirs + [str(HERE / "skills")]
    nw = NetwinsProvider(home=str(home))
    agent = build_agent(s, [AgenticMCPProvider(), nw], middleware=[TwinGate()])
    who = Identity("operator", "admin")
    print(f"model {models.describe()['model']} · {agent.info['tools']} tools · subagents {agent.info['subagents']}",
          flush=True)
    only = set(filter(None, a.only.split(",")))
    rows = []
    for cid, turns in [c for c in CASES if not only or c[0] in only]:
        thread, t0 = f"livectl-{int(time.time())}-{cid}", time.time()
        before = set(nw.checker.checks)
        proposed, text, calls, err = [], "", [], ""
        try:
            for turn in turns:
                r = agent.ask(turn, thread_id=thread, who=who)
                for _ in range(4):
                    if not r.pending:
                        break
                    proposed += [{"tool": p["name"], "args": p.get("args")} for p in r.pending]
                    r = agent.decide(thread, [approval.decision("reject", message="test run: nothing changes on the lab")
                                              for _ in r.pending], who=who)
                calls += r.tool_calls
                text = r.text or ""
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        checks = [{k: v.get(k) for k in ("controller", "tool", "arguments", "action", "reasons")}
                  for k2, v in nw.checker.checks.items() if k2 not in before]
        rows.append({"case": cid, "seconds": round(time.time() - t0, 1), "calls": [c["name"] for c in calls],
                     "proposed_and_rejected": proposed, "twin_checks": checks, "answer": text, "error": err})
        print(f"\n## {cid} ({rows[-1]['seconds']}s){' ERROR ' + err if err else ''}\n calls: {rows[-1]['calls']}"
              f"\n proposed (rejected): {[p['tool'] for p in proposed]}\n twin: {[(c['tool'], c['action']) for c in checks]}"
              f"\n answer: {text[:700]}", flush=True)
    agent.close()
    out = HERE / "integrations" / "results"
    out.mkdir(exist_ok=True)
    f = out / f"canah-live-ctl-{time.strftime('%Y%m%d-%H%M%S')}.json"
    f.write_text(json.dumps({"model": models.describe()["model"], "runs": rows}, indent=1, default=str))
    print(f"\n{f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
