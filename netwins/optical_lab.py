"""
The optical twin against the lab's physics: GNPy, given what an operator would know about a line, against
the line as the lab's Physical Layer Simulator worked it out (edgeLM physical_layer, its kept runs).

The lab's optical devices are emulated; the simulator is what stands where the glass would be (amplifier
blocks with gain windows and flattening filters, fibres, ASE and EGN nonlinear noise, with monitors that
measure like real ones). Each kept run holds the line (blocks, fibre lengths, launch power, grid, symbol
rate) and every channel's SNR, plus the amplifiers' *measured* gain and noise figure.

  lab_runs(folder)          the kept runs: line, the lab's mean SNR, the measured gain and NF per block
  predict(run)              GNPy on the same line, from what was measured (gain mode: the measured gains,
                            no amplifiers added; a fixed-gain amplifier per measured NF; the launch power
                            into the first span; the grid and symbol rate): the mean SNR in the signal's
                            bandwidth over the same channels
  compare(folder)           both, per run: GNPy's error, which is what calibration learns from

Each amplifier's gain is set so that the power into each span is what the lab's power monitors report
for it (the line's launch powers, averaged over the channels): GNPy sees the same power profile and
works out its own noise. GNPy's fibre is SSMF at 0.2 dB/km (the lab's figure); connector losses, the
gain ripple and the ROADMs' internals are not known from a line, which is part of what the residuals are.
"""
from __future__ import annotations

import csv
import glob
import json
import logging
import math
import os
import tempfile
from statistics import mean
from typing import Any, Dict, List, Optional

# The folder of kept runs: one sub-folder per line, each with line.json, result.json, channel_powers.csv and
# amplifier_monitoring.csv (edgeLM's physical_layer/data, or anything written in that form).
LAB_DATA = os.getenv("NETWINS_LAB_OPTICAL", "")
LOSS_DB_PER_KM = 0.2


def lab_runs(folder: str = "") -> List[Dict[str, Any]]:
    folder = folder or LAB_DATA
    if not folder or not os.path.isdir(folder):
        raise FileNotFoundError("no lab runs: set NETWINS_LAB_OPTICAL to the folder of kept physical-layer runs")
    out = []
    for d in sorted(glob.glob(os.path.join(folder, "*", ""))):
        try:
            with open(os.path.join(d, "line.json")) as fh:
                line = json.load(fh)
            with open(os.path.join(d, "result.json")) as fh:
                result = json.load(fh)
        except (OSError, ValueError):
            continue
        amps: Dict[int, Dict[str, List[float]]] = {}
        try:
            with open(os.path.join(d, "amplifier_monitoring.csv")) as fh:
                for r in csv.DictReader(fh):
                    a = amps.setdefault(int(float(r["block"])), {"g": [], "nf": []})
                    a["g"].append(float(r["measured_gain_dB"]))
                    a["nf"].append(float(r["measured_nf_dB"]))
        except (OSError, KeyError, ValueError):
            amps = {}
        launch: Dict[int, List[float]] = {}
        try:
            with open(os.path.join(d, "channel_powers.csv")) as fh:
                for r in csv.DictReader(fh):
                    launch.setdefault(int(r["fiber"].split("_")[-1]), []).append(float(r["launch_power_dBm"]))
        except (OSError, KeyError, ValueError):
            launch = {}
        if result.get("snr_mean_db") is None or not line.get("fibres") or not launch:
            continue
        out.append({"key": os.path.basename(os.path.dirname(d)), "name": line.get("name", ""), "line": line,
                    "lab_snr_db": float(result["snr_mean_db"]),
                    "launch_dbm": {i: _db_mean(v) for i, v in launch.items()},
                    "measured": {b: {"gain_db": mean(v["g"]), "nf_db": mean(v["nf"])} for b, v in amps.items() if v["g"]}})
    return out


def _db_mean(values: List[float]) -> float:
    """The mean power of channels, in dBm (averaged in linear units)."""
    return 10 * math.log10(sum(10 ** (v / 10) for v in values) / len(values))


class GnpyLab:
    """GNPy with fixed-gain amplifiers at every measured noise figure, made once."""

    NF_STEP = 0.25

    def __init__(self):
        import gnpy
        from pathlib import Path
        from gnpy.core.parameters import SimParams
        from gnpy.tools.json_io import load_equipments_and_configs
        logging.getLogger("gnpy").setLevel(logging.ERROR)
        base = json.loads((Path(gnpy.__file__).parent / "example-data" / "eqpt_config.json").read_text())
        nfs = [round(3.0 + i * self.NF_STEP, 2) for i in range(int((9.0 - 3.0) / self.NF_STEP) + 1)]
        base["Edfa"] += [{"type_variety": self._variety(nf), "type_def": "fixed_gain", "gain_flatmax": 40, "gain_min": 0,
                          "p_max": 30, "nf0": nf, "allowed_for_design": False} for nf in nfs]
        base["Span"][0]["power_mode"] = False
        # The lab's physics counts amplifier ASE and fibre nonlinearity, not a ROADM's add/drop noise or the
        # transmitter's own OSNR: GNPy's example equipment adds both (38 and 40 dB), which caps a short line
        # near 30 dB. Left out here, so the two count the same noise.
        base["Roadm"][0]["add_drop_osnr"] = 100
        for si in base["SI"]:
            si["tx_osnr"] = 100
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(base, fh)
            self.path = fh.name
        self._load = lambda: load_equipments_and_configs(Path(self.path), [], [])
        SimParams.set_params({})

    def _variety(self, nf: float) -> str:
        nf = min(9.0, max(3.0, round(nf / self.NF_STEP) * self.NF_STEP))
        return f"lab_nf_{nf:.2f}"

    def predict(self, run: Dict[str, Any]) -> Optional[float]:
        import numpy as np
        from gnpy.tools.json_io import network_from_json
        from gnpy.tools.worker_utils import designed_network, transmission_simulation
        line, meas = run["line"], run["measured"]
        sig = line["signal"]
        eq = self._load()
        si = eq["SI"]["default"]
        si.baud_rate = float(sig["symb_rate_gbaud"]) * 1e9
        si.spacing = float(sig["channel_spacing"]) * 1e9
        si.f_min = float(sig["fch_start"]) * 1e9
        si.f_max = float(sig["fch_stop"]) * 1e9
        si.power_dbm = float(sig["signal_pch"])
        si.roll_off = max(0.01, float(sig.get("roll_off") or 0.0))
        blocks, fibres = line["blocks"], line["fibres"]
        launch = run["launch_dbm"]                    # per span, as the lab's power monitors report it
        gains, booster = [], self._gain(blocks, meas, 0)
        for i in range(len(blocks)):
            if i < len(fibres) and (i + 1) in launch:
                into = (launch[1] - booster) if i == 0 else launch[i] - LOSS_DB_PER_KM * float(fibres[i - 1]["length_km"])
                gains.append(launch[i + 1] - into)
            elif i == len(blocks) - 1 and fibres:          # the preamp: makes up the last span
                gains.append(LOSS_DB_PER_KM * float(fibres[-1]["length_km"]))
            else:
                gains.append(self._gain(blocks, meas, i))
        el = [{"uid": "trx A", "type": "Transceiver"}, {"uid": "trx B", "type": "Transceiver"},
              {"uid": "roadm A", "type": "Roadm", "params": {"target_pch_out_db": launch.get(1, float(sig["signal_pch"])) - booster}},
              {"uid": "roadm B", "type": "Roadm"}]
        conn = [{"from_node": "trx A", "to_node": "roadm A"}, {"from_node": "roadm B", "to_node": "trx B"}]
        prev = "roadm A"
        for i, b in enumerate(blocks):                 # block i, then the fibre after it (if any)
            uid = f"amp {i}"
            el.append({"uid": uid, "type": "Edfa", "type_variety": self._variety(self._nf(meas, i)),
                       "operational": {"gain_target": round(gains[i], 4), "tilt_target": 0, "out_voa": 0}})
            conn.append({"from_node": prev, "to_node": uid})
            prev = uid
            if i < len(fibres):
                fu = f"fibre {i}"
                el.append({"uid": fu, "type": "Fiber", "type_variety": "SSMF",
                           "params": {"length": float(fibres[i]["length_km"]), "length_units": "km",
                                      "loss_coef": LOSS_DB_PER_KM, "con_in": 0.0, "con_out": 0.0}})
                conn.append({"from_node": prev, "to_node": fu})
                prev = fu
        conn.append({"from_node": prev, "to_node": "roadm B"})
        net = network_from_json({"elements": el, "connections": conn}, eq)
        net, req, ref = designed_network(eq, net, "trx A", "trx B", nodes_list=["trx B"], loose_list=["STRICT"],
                                         args_power=float(sig["signal_pch"]), no_insert_edfas=True)
        path, *_ = transmission_simulation(eq, net, req, ref)
        return float(np.mean(np.asarray(path[-1].snr)))

    @staticmethod
    def _gain(blocks, meas, i) -> float:
        m = meas.get(i + 1)
        if m:
            return float(m["gain_db"])
        b = blocks[i]
        return float(b.get("requested_gain_db") or b.get("gff_flat_gain_db") or 18.0)

    @staticmethod
    def _nf(meas, i) -> float:
        m = meas.get(i + 1)
        return float(m["nf_db"]) if m else 5.5


def compare(folder: str = "", limit: int = 0) -> List[Dict[str, Any]]:
    g = GnpyLab()
    out = []
    for run in lab_runs(folder)[: limit or None]:
        try:
            p = g.predict(run)
            err = None
        except Exception as e:  # noqa: BLE001 - kept, said
            p, err = None, f"{type(e).__name__}: {str(e)[:160]}"
        sig = run["line"]["signal"]
        out.append({"key": run["key"], "name": run["name"], "spans": len(run["line"]["fibres"]),
                    "km": sum(f["length_km"] for f in run["line"]["fibres"]), "baud": sig["symb_rate_gbaud"],
                    "launch_dbm": sig["signal_pch"], "lab_snr_db": round(run["lab_snr_db"], 3),
                    "gnpy_snr_db": None if p is None else round(p, 3),
                    "error_db": None if p is None else round(p - run["lab_snr_db"], 3), "problem": err})
    return out


def calibrate(cal: Any, rows: Optional[List[Dict[str, Any]]] = None, domain: str = "optical") -> Dict[str, Any]:
    """The lab's lines into the optical twin's calibration: each run's error is the error of every QoT figure
    the twin reports from that SNR (the GSNR in 0.1 nm and the margin move with it, one for one)."""
    rows = rows if rows is not None else compare()
    n = 0
    for r in rows:
        if r["gnpy_snr_db"] is None:
            continue
        p = r["gnpy_snr_db"]
        for kpi in ("gsnr_db", "margin_db"):
            cal.add(domain, kpi, {"p5": p, "p50": p, "p95": p}, r["lab_snr_db"], "lab",
                    features=f"{r['key']} {r['spans']} spans {r['baud']} GBd {r['launch_dbm']} dBm")
        n += 1
    return {"runs": n, "status": {k: cal.status(domain, k) for k in ("gsnr_db", "margin_db")}}


def report(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """GNPy against the lab, raw and calibrated (leave-one-out: each run predicted from all the others)."""
    import numpy as np
    from .calibration import Calibration
    ok = [r for r in rows if r["error_db"] is not None]
    e = np.array([r["error_db"] for r in ok])
    resid = -e                                     # lab - gnpy
    loo_bias = np.array([np.delete(resid, i).mean() for i in range(len(resid))])
    corrected = resid - loo_bias                  # what remains after the bias learned from the others
    cal = Calibration()
    for r in ok:
        cal.add("optical", "gsnr_db", {"p5": r["gnpy_snr_db"], "p50": r["gnpy_snr_db"], "p95": r["gnpy_snr_db"]}, r["lab_snr_db"], "lab")
    q = cal.q("optical", "gsnr_db", 0.1)
    return {"runs": len(ok), "raw": {"bias_db": round(float(e.mean()), 2), "mae_db": round(float(np.abs(e).mean()), 2),
                                     "std_db": round(float(e.std(ddof=1)), 2)},
            "bias_corrected_loo": {"mae_db": round(float(np.abs(corrected).mean()), 2),
                                   "within_1db": round(float((np.abs(corrected) <= 1).mean()), 3),
                                   "within_2db": round(float((np.abs(corrected) <= 2).mean()), 3)},
            "conformal_90": {"half_width_db": None if q is None else round(q, 2),
                             "coverage_loo": cal.status("optical", "gsnr_db").get("coverage_conformal_90")},
            "tier": cal.status("optical", "gsnr_db")["trust_tier"]}
