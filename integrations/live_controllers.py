"""
Read-only: mirror every controller the agentic MCP holds into a twin snapshot, and report what the twin
got from each (measured or assumed), without a model and without writing anything.

  CANAH_ROOT=~/CANAH ~/CANAH/.venv/bin/python integrations/live_controllers.py [--only onos,ryu] [--out DIR]

The agentic MCP's URL and token come from CANAH's settings, as they do for a CANAH process (CANAH loads
its own .env); nothing is printed of them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CANAH = Path(os.getenv("CANAH_ROOT", str(Path.home() / "CANAH"))).resolve()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--interval", type=float, default=5.0)
    a = ap.parse_args(argv)
    sys.path[:0] = [str(HERE), str(CANAH)]
    import canah.models  # noqa: F401 - loads CANAH's .env, as a CANAH process does
    from netwins import mirror
    from netwins.source import AgenticSource, verbs

    out = Path(a.out or tempfile.mkdtemp(prefix="netwins-live-"))
    src = AgenticSource()
    rows = src.controllers()
    only = set(filter(None, a.only.split(",")))
    report = []
    for row in rows:
        key = row.get("key")
        if only and key not in only:
            continue
        entry = {"controller": key, "domain": row.get("domain"), "verdict": (row.get("conformance") or {}).get("verdict")}
        t0 = time.time()
        try:
            tools = src.tools(key)
            v = verbs(row, tools)
            entry["verbs"] = v
            entry["writes"] = sorted(t["name"] for t in tools if t.get("write"))[:40]
            snap = mirror.mirror(src, key, interval_s=a.interval)
            mirror.save(snap, out)
            cap, why = mirror.trust_caps(snap)
            t = snap["topology"]
            entry.update(ok=True, seconds=round(time.time() - t0, 1), snapshot=snap["snapshot_id"],
                         nodes=len(t["nodes"]), links=len(t["links"]),
                         demands=len(snap["traffic"]["demands"]),
                         sample_links=t["links"][:4], sample_demands=snap["traffic"]["demands"][:4],
                         coverage=snap["coverage"], provenance=snap["provenance"][:12],
                         best_tier=cap or "A", why=why)
        except Exception as e:  # noqa: BLE001 - reported per controller
            entry.update(ok=False, seconds=round(time.time() - t0, 1), error=f"{type(e).__name__}: {e}",
                         trace=traceback.format_exc()[-1200:])
        report.append(entry)
        print(json.dumps(entry, indent=1, default=str)[:3500], flush=True)
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    print(f"\nsnapshots and report in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
