"""The optical and Wi-Fi twins, through the same pipeline as IP: schema, semantics, physics that must hold,
and (where the lab's data and the ns-3 image are present) validation against the lab and real ns-3."""
import copy
import json
import os
import subprocess
import unittest

from netwins import domains, optical, wifi
from netwins.calibration import Calibration
from netwins.runner import FakeEngine
from netwins.twin import Twin


def ex(module, **over):
    s = copy.deepcopy(module.EXAMPLE)
    s.update(over)
    return s


class Registry(unittest.TestCase):
    def test_every_domain_has_a_valid_worked_example(self):
        for name in domains.names():
            d = domains.get(name)
            self.assertEqual(d.problems(domains.parse(d.example)), [], name)

    def test_a_scenario_is_read_by_its_domain(self):
        self.assertIsInstance(domains.parse(optical.EXAMPLE), optical.OScenario)
        self.assertIsInstance(domains.parse(wifi.EXAMPLE), wifi.WScenario)
        with self.assertRaises(ValueError):
            domains.get("satellite")

    def test_validate_says_what_is_wrong_in_each_domain(self):
        t = Twin(FakeEngine())
        bad = ex(optical)
        bad["traffic"]["lightpaths"][0]["path"] = ["F-cb"]           # does not start at A
        bad["requirements"][0]["kpi"] = "throughput_mbps"            # not a lightpath KPI
        got = t.validate(bad)
        self.assertFalse(got["ok"])
        self.assertTrue(any("not continuous" in p for p in got["problems"]), got)
        self.assertTrue(any("a lightpath has" in p for p in got["problems"]), got)
        bad = ex(wifi)
        bad["topology"]["aps"][0]["channel"] = 37
        bad["traffic"]["demands"][0]["dst"] = "laptop"               # station to station
        got = t.validate(bad)
        self.assertTrue(any("not a 20 MHz channel" in p for p in got["problems"]), got)
        self.assertTrue(any("one end must be" in p for p in got["problems"]), got)


class Optical(unittest.TestCase):
    def run_one(self, sc):
        b, c = domains.get("optical").baseline_and_changed(domains.parse(sc))
        e = optical.OpticalEngine()
        return e.run(b, 1)["demands"], e.run(c, 1)["demands"]

    def test_longer_lines_have_less_margin(self):
        e = optical.OpticalEngine()
        got = []
        for km in (80, 400, 1600):
            sc = ex(optical, change=None)
            sc["topology"]["links"][0]["length_km"] = km
            got.append(e.run(domains.parse(sc).model_dump(), 1)["demands"]["LP-1"]["gsnr_db"])
        self.assertGreater(got[0], got[1])
        self.assertGreater(got[1], got[2])

    def test_a_cut_reroutes_a_restoring_lightpath_and_downs_one_that_does_not(self):
        base, cut = self.run_one(ex(optical))
        self.assertEqual((base["LP-1"]["hops"], cut["LP-1"]["hops"]), (1, 2))
        self.assertLess(cut["LP-1"]["gsnr_db"], base["LP-1"]["gsnr_db"])
        self.assertGreater(cut["LP-1"]["path_km"], base["LP-1"]["path_km"])
        sc = ex(optical)
        sc["traffic"]["lightpaths"][0]["restore"] = False
        _, cut = self.run_one(sc)
        self.assertEqual(cut["LP-1"]["up"], 0)
        self.assertEqual(cut["acme"]["loss_ratio"], 1.0)

    def test_a_faster_mode_needs_more_osnr(self):
        sc = ex(optical, change=None)
        e = optical.OpticalEngine()
        sc["traffic"]["lightpaths"][0]["rate_gbps"] = 100
        m100 = e.run(domains.parse(sc).model_dump(), 1)["demands"]["LP-1"]["margin_db"]
        sc["traffic"]["lightpaths"][0]["rate_gbps"] = 400
        m400 = e.run(domains.parse(sc).model_dump(), 1)["demands"]["LP-1"]["margin_db"]
        self.assertAlmostEqual(m100 - m400, 21 - 12, delta=0.01)      # the modes' required OSNR differ by 9 dB

    def test_the_pipeline_runs_the_optical_twin(self):
        env = Twin(FakeEngine()).predict(optical.EXAMPLE)
        self.assertTrue(env["ok"], env)
        self.assertEqual(env["engine_versions"]["engine_kind"], "gnpy")
        self.assertIn("margin_db", env["prediction"]["LP-1"])
        self.assertEqual(env["p_meets_requirement"]["p"], 1.0)
        self.assertEqual(env["decision"]["action"], "gather_more_data")   # uncalibrated: may not let it go ahead
        self.assertIn("GNPy", env["not_modelled"])


LAB = os.path.isdir(os.getenv("NETWINS_LAB_OPTICAL", "") or "/nonexistent")


@unittest.skipUnless(LAB, "set NETWINS_LAB_OPTICAL to the lab's physical-layer runs to compare against them")
class OpticalAgainstTheLab(unittest.TestCase):
    """Phase 4's exit criterion: GNPy against the lab's physics, raw and calibrated."""

    @classmethod
    def setUpClass(cls):
        from netwins import optical_lab
        cls.rows = optical_lab.compare()
        cls.report = optical_lab.report(cls.rows)

    def test_every_kept_line_is_predicted(self):
        self.assertGreater(len(self.rows), 100)
        self.assertTrue(all(r["error_db"] is not None for r in self.rows), [r["problem"] for r in self.rows if r["problem"]][:3])

    def test_calibration_makes_the_twin_honest(self):
        r = self.report
        self.assertGreater(r["raw"]["mae_db"], r["bias_corrected_loo"]["mae_db"])     # the bias is learned
        self.assertGreaterEqual(r["conformal_90"]["coverage_loo"], 0.85)              # and the interval holds
        self.assertLess(r["conformal_90"]["half_width_db"], 6.0)


class WifiModel(unittest.TestCase):
    def test_a_station_joins_the_access_point_it_hears_loudest(self):
        v = wifi.engine_view(domains.parse(wifi.EXAMPLE).model_dump())
        self.assertEqual({s["id"]: s["ap"] for s in v["stations"]}, {"cam": "ap-1", "laptop": "ap-2"})

    def test_the_pipeline_runs_the_wifi_twin_on_its_fake(self):
        env = Twin(FakeEngine()).predict(wifi.EXAMPLE)
        self.assertTrue(env["ok"], env)
        self.assertEqual(env["engine_versions"]["engine_kind"], "fake-wifi")
        self.assertEqual(set(env["prediction"]), {"camera", "web"})

    def test_a_sampled_path_loss_exponent_reaches_the_members(self):
        from netwins.runner import members
        sc = domains.parse(wifi.EXAMPLE)
        got = {m[1]["topology"]["radio"]["path_loss_exponent"] for m in members(sc, 0, 6)}
        self.assertEqual(len(got), 6)
        self.assertTrue(all(2.8 <= x <= 3.4 for x in got))


IMAGE = subprocess.run(["docker", "--context", "default", "image", "inspect", "netwins-ns3:3.43-r2"],
                       capture_output=True).returncode == 0


@unittest.skipUnless(IMAGE, "the netwins-ns3 image is not built")
class WifiOnNs3(unittest.TestCase):
    """Phase 5's exit criterion: controlled scenarios where the answer's direction is known."""

    def run_one(self, sc, seed=1):
        from netwins.runner import DockerEngine
        e = DockerEngine(program="netwins-wifi", view=wifi.engine_view)
        sc = domains.parse(sc)
        b, c = domains.get("wifi").baseline_and_changed(sc)
        return e.run(c, seed)

    def base(self, **over):
        sc = {"schema_version": "1.0", "domain": "wifi", "duration_s": 6,
              "topology": {"aps": [{"id": "ap-1", "x": 0, "y": 0, "channel": 36}], "stations": [],
                           "radio": {"standard": "ax", "path_loss_exponent": 3.0}},
              "traffic": {"demands": []}}
        sc.update(over)
        return sc

    def test_a_far_station_gets_less_than_a_near_one(self):
        sc = self.base()
        sc["topology"]["stations"] = [{"id": "near", "x": 5, "y": 0}]
        sc["traffic"]["demands"] = [{"id": "d", "src": "server", "dst": "near", "protocol": "udp", "rate_mbps": 60, "start_s": 1}]
        near = self.run_one(sc)["demands"]["d"]
        sc["topology"]["stations"] = [{"id": "near", "x": 140, "y": 0}]
        far = self.run_one(sc)["demands"]["d"]
        self.assertLess(near["loss_ratio"], 0.02)
        self.assertGreater(far["loss_ratio"], near["loss_ratio"] + 0.2)

    def test_stations_share_their_access_points_airtime(self):
        sc = self.base()
        sc["topology"]["stations"] = [{"id": f"s{i}", "x": 30 + i, "y": 0} for i in range(4)]
        sc["traffic"]["demands"] = [{"id": f"d{i}", "src": "server", "dst": f"s{i}", "protocol": "udp", "rate_mbps": 60, "start_s": 1}
                                    for i in range(4)]
        got = self.run_one(sc)["demands"]
        alone = self.run_one({**sc, "topology": {**sc["topology"], "stations": sc["topology"]["stations"][:1]},
                              "traffic": {"demands": sc["traffic"]["demands"][:1]}})["demands"]["d0"]
        total = sum(v["throughput_mbps"] for v in got.values())
        self.assertLess(total, 4 * alone["throughput_mbps"] * 0.9)          # four do not get four times one

    def test_a_station_roams_when_its_access_point_fails(self):
        sc = self.base(duration_s=12)
        sc["topology"]["aps"].append({"id": "ap-2", "x": 30, "y": 0, "channel": 36})
        sc["topology"]["stations"] = [{"id": "cam", "x": 10, "y": 2}]
        sc["traffic"]["demands"] = [{"id": "cam", "src": "cam", "dst": "server", "protocol": "udp", "rate_mbps": 5, "start_s": 1}]
        sc["change"] = {"events": [{"t": 5, "type": "ap_down", "target": "ap-1"}]}
        got = self.run_one(sc)
        self.assertEqual(got["stations"]["cam"]["ap_at_end"], "ap-2")
        self.assertGreater(got["demands"]["cam"]["loss_ratio"], 0.0)          # the gap before it re-associates
        self.assertLess(got["demands"]["cam"]["loss_ratio"], 0.5)             # but not the rest of the run
        # On another channel it cannot roam: the rest of the run is lost.
        sc["topology"]["aps"][1]["channel"] = 149
        got = self.run_one(sc)
        self.assertGreater(got["demands"]["cam"]["loss_ratio"], 0.5)


if __name__ == "__main__":
    unittest.main()


class NTN(unittest.TestCase):
    """Satellites and HAPS: geometry, link budget, the what-ifs."""

    def setUp(self):
        import copy
        from netwins.ntn import EXAMPLE
        self.ex = copy.deepcopy(EXAMPLE)
        self.ex["seed_policy"] = {"runs": 2, "max_runs": 2}
        self.ex["uncertain_params"] = []

    def run_member(self, sc):
        from netwins import domains
        d = domains.get("ntn")
        base, changed = d.baseline_and_changed(d.model.model_validate(sc))
        e = d.engine("fake")
        return e.run(base, 0)["demands"], e.run(changed, 0)["demands"]

    def test_the_example_is_valid_and_rain_costs_availability(self):
        from netwins import domains
        d = domains.get("ntn")
        self.assertEqual(d.problems(d.model.model_validate(self.ex)), [])
        base, rain = self.run_member(self.ex)
        self.assertGreater(base["backhaul-1"]["availability"], 0.95)
        self.assertLess(rain["backhaul-1"]["availability"], base["backhaul-1"]["availability"])
        self.assertLess(base["backhaul-1"]["delay_ms_mean"], 40)          # LEO: tens of ms

    def test_a_geo_link_takes_a_quarter_second_each_way(self):
        sc = dict(self.ex)
        sc["topology"] = {"sites": self.ex["topology"]["sites"],
                          "satellites": [{"id": "GEO-13E", "kind": "geo", "altitude_km": 35786, "phase_deg": 13.0,
                                          "eirp_dbw": 60, "g_t_dbk": 15}]}
        sc["change"] = None
        sc["radio"] = {"uplink_ghz": 30, "downlink_ghz": 20, "bandwidth_mhz": 36}   # a GEO carrier, not a LEO beam
        sc["topology"]["sites"][0]["eirp_dbw"] = 55
        for s in sc["topology"]["sites"]:
            s["min_elevation_deg"] = 10
        base, _ = self.run_member(sc)
        self.assertGreater(base["backhaul-1"]["delay_ms_mean"], 240)
        self.assertEqual(base["backhaul-1"]["handovers"], 0)

    def test_losing_the_inter_satellite_links_or_the_gateway(self):
        sc = dict(self.ex)
        sc["change"] = {"events": [{"t": 0, "type": "site_down", "target": "GW-fucino"}]}
        _, down = self.run_member(sc)
        self.assertEqual(down["backhaul-1"]["availability"], 0)
        self.assertEqual(down["backhaul-1"]["throughput_mbps"], 0)

    def test_rain_fade_grows_with_frequency_and_rain(self):
        from netwins.ntn import rain_db
        self.assertLess(rain_db(12, 25, 40), rain_db(30, 25, 40))
        self.assertLess(rain_db(20, 5, 40), rain_db(20, 50, 40))
        self.assertEqual(rain_db(20, 0, 40), 0.0)

    def test_problems_are_said(self):
        from netwins import domains
        d = domains.get("ntn")
        bad = dict(self.ex)
        bad["change"] = {"events": [{"t": 0, "type": "satellite_down", "target": "nope"},
                                    {"t": 0, "type": "rain", "target": "T-turin"}]}
        got = d.problems(d.model.model_validate(bad))
        self.assertTrue(any("no satellite 'nope'" in p for p in got), got)
        self.assertTrue(any("mm/h" in p for p in got), got)

    def test_the_twin_describes_it_and_predicts_through_the_contract(self):
        from netwins.contract import twin
        t = twin()
        doms = {d["domain"]: d for d in t.describe()["domains"]}
        self.assertIn("ntn", doms)
        self.assertIn("availability", doms["ntn"]["kpis"])
        r = t.predict(self.ex)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["decision"]["action"], "reject")              # 25 mm/h on a Ka uplink
