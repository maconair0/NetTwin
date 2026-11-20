"""
  python -m netwins validate SCENARIO.json
  python -m netwins predict SCENARIO.json [--engine docker|fake|local:<binary>] [--full]
  python -m netwins calibrate-synthetic [--n 30] [--engine ...]     fill the calibration store (synthetic)
  python -m netwins status                                         calibration and snapshots
  python -m netwins schema                                         the scenario JSON Schema

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
from .schema import json_schema
from .twin import Twin


def engine_of(spec: str):
    if spec == "fake":
        return FakeEngine()
    if spec.startswith("local:"):
        return LocalEngine(spec.split(":", 1)[1])
    return DockerEngine()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="netwins")
    ap.add_argument("command", choices=["validate", "predict", "calibrate-synthetic", "status", "schema"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--engine", default=os.getenv("NETWINS_ENGINE", "docker"))
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args(argv)
    home = Path(os.getenv("NETWINS_HOME", "netwins_data"))
    home.mkdir(parents=True, exist_ok=True)
    cal = Calibration(home / "calibration.sqlite")
    if a.command == "schema":
        print(json.dumps(json_schema(), indent=1))
        return 0
    if a.command == "status":
        print(json.dumps({"snapshots": state.domains(),
                          "calibration": {k: cal.status("ip", k) for k in ("throughput_mbps", "delay_ms_mean",
                                                                            "jitter_ms_mean", "loss_ratio")}},
                         indent=1, default=str))
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
