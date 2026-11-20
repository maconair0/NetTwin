"""
  python -m netwins validate SCENARIO.json
  python -m netwins predict SCENARIO.json [--engine docker|fake|local:<binary>] [--full]
  python -m netwins calibrate-synthetic [--n 30] [--engine ...]     fill the IP calibration store (synthetic)
  python -m netwins calibrate-optical-lab                          the optical twin against the lab's physics:
                                                                   its error per line, into the calibration store
  python -m netwins status                                         calibration (every twin) and snapshots
  python -m netwins schema [--domain ip|optical|wifi]              a twin's scenario JSON Schema
  python -m netwins serve [--host 127.0.0.1] [--port 8420]         the canah.twin/1 contract over HTTP

A scenario file names its own domain ("domain": "optical"), and predict runs that twin.

State lives in NETWINS_HOME (default ./netwins_data): calibration.sqlite, runs/.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from . import state
from .calibration import Calibration
from .runner import DockerEngine, FakeEngine, LocalEngine
from .twin import Twin


def engine_of(spec: str):
    if spec == "fake":
        return FakeEngine()
    if spec.startswith("local:"):
        return LocalEngine(spec.split(":", 1)[1])
    return DockerEngine()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="netwins")
    ap.add_argument("command", choices=["validate", "predict", "calibrate-synthetic", "calibrate-optical-lab", "status", "schema", "serve"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--domain", default="ip")
    ap.add_argument("file", nargs="?")
    ap.add_argument("--engine", default=os.getenv("NETWINS_ENGINE", "docker"))
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args(argv)
    home = Path(os.getenv("NETWINS_HOME", "netwins_data"))
    home.mkdir(parents=True, exist_ok=True)
    cal = Calibration(home / "calibration.sqlite")
    from . import domains
    if a.command == "serve":
        import logging
        from .service import serve
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        srv = serve(a.host, a.port)
        print(f"netwins (canah.twin/1) on http://{a.host}:{a.port}/twin", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()
        return 0
    if a.command == "schema":
        print(json.dumps(domains.get(a.domain).model.model_json_schema(), indent=1))
        return 0
    if a.command == "status":
        kpis = {"ip": ("throughput_mbps", "delay_ms_mean", "jitter_ms_mean", "loss_ratio"),
                "optical": ("gsnr_db", "margin_db"), "wifi": ("throughput_mbps", "delay_ms_mean", "loss_ratio")}
        print(json.dumps({"snapshots": state.domains(),
                          "calibration": {d: {k: cal.status(d, k) for k in ks} for d, ks in kpis.items()}},
                         indent=1, default=str))
        return 0
    if a.command == "calibrate-optical-lab":
        from . import optical_lab
        rows = optical_lab.compare()
        got = optical_lab.calibrate(cal, rows)
        print(json.dumps({"against_the_lab": optical_lab.report(rows), "calibration": got["status"]}, indent=1, default=str))
        return 0
    twin = Twin(engine_of(a.engine), cal)
    if a.command == "calibrate-synthetic":
        from .fidelity import calibrate
        print(json.dumps(calibrate(twin.engine, cal, "ip", a.n), indent=1, default=str))
        return 0
    if not a.file:
        print("a scenario file is needed")
        return 2
    text = Path(a.file).read_text()
    if a.command == "validate":
        got = twin.validate(text)
        print(json.dumps(got, indent=1))
        return 0 if got["ok"] else 1
    env = twin.predict(text)
    if a.full or not env.get("ok"):
        print(json.dumps(env, indent=1, default=str))
    else:
        from .twin import compact
        print(json.dumps(compact(env), indent=1, default=str))
    return 0 if env.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
