"""The live loop: mirror a controller, translate a write, check it in the twin, hold it at the gate."""
import json
import os
import tempfile
import time
import unittest

import numpy as np

from netwins import changes, mirror, state
from netwins.calibration import Calibration
from netwins.gate import Checker, summary
from netwins.runner import FakeEngine
from netwins.source import AgenticSource
from netwins.testing.sdn_lab import serve
from netwins.twin import Twin


def calibrated() -> Calibration:
    cal = Calibration()
    rng = np.random.default_rng(1)
    for _ in range(40):
        cal.add("ip", "loss_ratio", {"p5": 0, "p50": 0, "p95": 0}, float(rng.uniform(-0.001, 0.001)), "testbed")
    return cal


class Lab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.url, cls.lab = serve()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.lab.reset()
        self.dir = tempfile.mkdtemp()
        os.environ["NETWINS_SNAPSHOTS"] = self.dir
        self.checker = Checker(Twin(FakeEngine(), calibrated()), AgenticSource(self.url), interval_s=0.5)

    def tearDown(self):
        from tests import SAMPLES
        os.environ["NETWINS_SNAPSHOTS"] = SAMPLES


class Slicing(unittest.TestCase):
    def test_traffic_that_crosses_the_slice_comes_with_it(self):
        from netwins import state
        got = state.subnetwork("ip", ["pe-a", "pe-b"], 1)
        ids = {d["id"] for d in got["traffic"]["demands"]}
        self.assertEqual(ids, {"acme", "globex", "backup"})      # globex and backup end at dc-1, outside
        self.assertIn("dc-1", {n["id"] for n in got["topology"]["nodes"]})


class Mirroring(Lab):
    def test_a_mirrored_scenario_runs_on_the_real_engine(self):
        import subprocess
        if subprocess.run(["docker", "--context", "default", "image", "inspect", "netwins-ns3:3.43-r2"],
                          capture_output=True).returncode != 0:
            self.skipTest("the netwins-ns3 image is not built")
        from netwins.runner import DockerEngine
        snap = mirror.mirror(AgenticSource(self.url), "lab", interval_s=0.5)
        t = changes.translate(snap, "provision", "add_path", {"name": "w", "from": "pe-a", "to": "dc-1",
                                                              "bandwidth_mbps": 10})
        sc = t["scenario"]
        sc["seed_policy"] = {"runs": 2, "max_runs": 2, "adaptive": False}
        env = Twin(DockerEngine(), calibrated()).predict(sc)
        self.assertTrue(env["ok"], env)
        self.assertEqual(env["p_meets_uncalibrated"]["p"], 1.0)       # 2 runs: too few to decide, all pass
        self.assertLess(env["prediction"]["w"]["loss_ratio"]["changed"]["mean"], 0.01)

    def test_the_mirror_reads_the_network_through_the_controllers_own_tools(self):
        snap = mirror.mirror(AgenticSource(self.url), "lab", interval_s=0.5)
        self.assertEqual(snap["network"], "lab")
        self.assertEqual(len(snap["topology"]["links"]), 6)
        self.assertEqual(snap["coverage"]["share_of_link_rates_measured"], 1.0)
        rates = {l["id"]: l["rate_mbps"] for l in snap["topology"]["links"]}
        self.assertEqual((rates["L-13"], rates["L-12"]), (40.0, 100.0))
        self.assertIn("get_wiring", snap["source"])
        self.assertEqual(snap["ports"]["L-12"], [["p-1", "2"], ["p-2", "1"]])

    def test_traffic_no_service_accounts_for_becomes_background(self):
        snap = mirror.mirror(AgenticSource(self.url), "lab", interval_s=0.5)
        bg = [d for d in snap["traffic"]["demands"] if d["id"].startswith("bg.")]
        self.assertEqual(len(bg), 1)
        self.assertEqual((bg[0]["src"], bg[0]["dst"]), ("pe-a", "p-1"))
        self.assertAlmostEqual(bg[0]["rate_mbps"], 5.0, delta=0.6)

    def test_a_link_that_is_down_is_left_out_and_said_so(self):
        self.lab.st["ports_down"].append(["p-1", 3])
        snap = mirror.mirror(AgenticSource(self.url), "lab", interval_s=0.2)
        self.assertNotIn("L-13", {l["id"] for l in snap["topology"]["links"]})
        self.assertTrue(any("left out" in p for p in snap["provenance"]))


class FakeSource:
    """A controller answering in some other controller's shapes."""
    def __init__(self, row, tools, answers):
        self.row, self._tools, self.answers = row, tools, answers
        self.n = 0

    def controllers(self):
        return [self.row]

    def tools(self, key):
        return self._tools

    def call(self, key, tool, args):
        self.n += 1
        got = self.answers[tool]
        return json.dumps(got(self.n) if callable(got) else got)


class Shapes(unittest.TestCase):
    def test_onos(self):
        tools = [{"name": n, "write": w, "schema": s} for n, w, s in [
            ("get_devices", False, {}), ("get_links", False, {}), ("get_all_intents", False, {}),
            ("get_device_ports", False, {"required": ["device_id"]}), ("get_port_statistics", False, {}),
            ("add_flow", True, {})]]
        row = {"key": "onos", "domain": "packet", "conformance": {"domains": [{"features": {
            "read_devices": "tool get_devices", "read_links": "tool get_links", "read_services": "tool get_all_intents",
            "read_ports": "tool get_device_ports", "read_statistics": "tool get_port_statistics"}}]}}
        d1, d2 = "of:0000000000000001", "of:0000000000000002"
        answers = {
            "get_devices": {"devices": [{"id": d1, "available": True}, {"id": d2, "available": True}]},
            "get_links": {"links": [{"src": {"port": "2", "device": d1}, "dst": {"port": "3", "device": d2}, "state": "ACTIVE"},
                                    {"src": {"port": "3", "device": d2}, "dst": {"port": "2", "device": d1}, "state": "ACTIVE"}]},
            "get_device_ports": {"ports": [{"port": "2", "portSpeed": 10000}, {"port": "3", "portSpeed": 1000}]},
            "get_all_intents": {"intents": []},
            "get_port_statistics": lambda n: {"statistics": [{"device": d1, "ports": [
                {"port": 2, "bytesSent": 1_000_000 * n, "bytesReceived": 0}]}]},
        }
        snap = mirror.mirror(FakeSource(row, tools, answers), "onos", interval_s=0.0, sleep=lambda s: None)
        self.assertEqual(len(snap["topology"]["links"]), 1)              # both directions, one link
        link = snap["topology"]["links"][0]
        self.assertEqual(link["rate_mbps"], 1000.0)                      # the slower port
        self.assertEqual({link["a"], link["z"]}, {d1, d2})
        self.assertEqual(snap["coverage"]["link_delay_measured"], 0)    # ONOS gives no latency
        self.assertTrue(any("assumed" in p and "latency" in p for p in snap["provenance"]))

    def test_ryu(self):
        tools = [{"name": "ryu_get_topology", "write": False, "schema": {}},
                 {"name": "ryu_list_switches", "write": False, "schema": {}}]
        row = {"key": "ryu", "domain": "packet", "conformance": {"domains": [{"features": {
            "read_devices": "tool ryu_list_switches", "read_links": "tool ryu_get_topology",
            "read_hosts": "tool ryu_get_topology"}}]}}
        answers = {"ryu_list_switches": [1, 2],
                   "ryu_get_topology": {"switches": [{"dpid": "0000000000000001"}],
                                        "links": [{"src": {"dpid": "0000000000000001", "port_no": "00000002"},
                                                   "dst": {"dpid": "0000000000000002", "port_no": "00000001"}}],
                                        "hosts": [{"mac": "00:00:00:00:00:01", "ipv4": ["10.0.0.1"],
                                                   "port": {"dpid": "0000000000000001", "port_no": "00000001"}}]}}
        snap = mirror.mirror(FakeSource(row, tools, answers), "ryu", interval_s=0.0, sleep=lambda s: None)
        ids = {n["id"] for n in snap["topology"]["nodes"]}
        self.assertIn("00:00:00:00:00:01", ids)
        self.assertEqual(snap["aliases"].get("10.0.0.1"), "00:00:00:00:00:01")
        cap, why = mirror.trust_caps(snap)
        self.assertEqual(cap, "C")                                       # no capacities: never let a change go ahead
        self.assertTrue(why)


class Translating(Lab):
    def snap(self):
        return mirror.mirror(AgenticSource(self.url), "lab", interval_s=0.2)

    def test_each_verb(self):
        s = self.snap()
        t = changes.translate(s, "provision", "add_path", {"name": "w", "from": "pe-a", "to": "dc-1", "bandwidth_mbps": 10})
        self.assertEqual(t["scenario"]["change"]["add_demands"][0]["rate_mbps"], 10.0)
        t = changes.translate(s, "teardown", "drop_path", {"name": "globex"})
        self.assertEqual(t["scenario"]["change"], {"remove_demands": ["globex"]})
        t = changes.translate(s, "adjust", "resize_path", {"name": "acme", "bandwidth_mbps": 50})
        self.assertEqual(t["scenario"]["change"]["set"][0]["path"], "demands.acme.rate_mbps")
        t = changes.translate(s, "configure_device", "set_port", {"dpid": "p-1", "port_no": 2, "admin_state": "down"})
        self.assertEqual(t["scenario"]["change"]["events"][0]["target"], "L-12")
        t = changes.translate(s, "configure_device", "set_port", {"dpid": "p-1", "port_no": 3, "admin_state": "down"})
        self.assertEqual(t["scenario"]["change"]["events"][0]["target"], "L-13")
        self.assertFalse(changes.translate(s, "reroute", "x", {})["modelled"])

    def test_what_the_write_leaves_out_becomes_a_question_and_the_answer_fills_it(self):
        s = self.snap()
        t = changes.translate(s, "provision", "add_path", {"name": "vid", "from": "pe-a", "to": "pe-b"})
        self.assertFalse(t["modelled"])
        self.assertIn("Mbit/s", t["questions"][0])
        t = changes.translate(s, "provision", "add_path", {"name": "vid", "from": "pe-a", "to": "pe-b"},
                              {"rate_mbps": 15})
        self.assertTrue(t["modelled"])
        t = changes.translate(s, "provision", "add_path", {"name": "x", "from": "pe-a", "to": "mars"})
        self.assertIn("'mars' not in the mirrored topology", t["questions"][0])


class Checking(Lab):
    def test_decisions(self):
        c = self.checker
        self.assertEqual(c.check("lab", "add_path", {"name": "w", "from": "pe-a", "to": "dc-1",
                                                     "bandwidth_mbps": 10})["action"], "propose")
        self.assertEqual(c.check("lab", "add_path", {"name": "b", "from": "pe-a", "to": "pe-b",
                                                     "bandwidth_mbps": 60})["action"], "reject")
        self.assertEqual(c.check("lab", "set_port", {"dpid": "p-1", "port_no": 2, "admin_state": "down"})["action"],
                         "reject")
        self.assertEqual(c.check("lab", "add_path", {"name": "v", "from": "pe-a", "to": "pe-b"})["action"],
                         "ask_operator")
        self.assertEqual(c.check("lab", "get_wiring", {})["action"], "not_modelled")

    def test_a_check_may_name_the_contract_verb_and_learns_the_controllers_tool(self):
        rec = self.checker.check("lab", "provision", {"name": "w", "from": "pe-a", "to": "dc-1", "bandwidth_mbps": 10})
        self.assertEqual((rec["tool"], rec["asked_as"], rec["action"]), ("add_path", "provision", "propose"))
        self.assertIn("lab__add_path", summary(rec)["send_as"])
        rec = self.checker.check("lab", "create_path_service", {"name": "w"})
        self.assertEqual(rec["action"], "unknown_tool")
        self.assertEqual(rec["write_tools"]["drop_path"], "teardown")

    def test_a_twin_that_fails_holds_the_write_and_says_why(self):
        class Broken:
            name = "broken"
            def run(self, member, seed):
                raise RuntimeError("engine failed (seed 1)")
        c = Checker(Twin(Broken(), calibrated()), AgenticSource(self.url), interval_s=0.2)
        rec = c.check("lab", "add_path", {"name": "w", "from": "pe-a", "to": "dc-1", "bandwidth_mbps": 10})
        self.assertEqual(rec["action"], "gather_more_data")
        self.assertIn("the twin failed", rec["reasons"][0])

    def test_uncalibrated_the_twin_rejects_but_never_lets_a_change_through(self):
        c = Checker(Twin(FakeEngine()), AgenticSource(self.url), interval_s=0.2)
        self.assertEqual(c.check("lab", "add_path", {"name": "w", "from": "pe-a", "to": "dc-1",
                                                     "bandwidth_mbps": 10})["action"], "gather_more_data")
        self.assertEqual(c.check("lab", "add_path", {"name": "b", "from": "pe-a", "to": "pe-b",
                                                     "bandwidth_mbps": 60})["action"], "reject")

    def test_a_stale_snapshot_is_mirrored_again(self):
        c = self.checker
        c.check("lab", "drop_path", {"name": "globex"})
        first = state.latest("lab")["snapshot_id"]
        snap = state.latest("lab")
        snap["taken_at"] = time.time() - 10_000
        mirror.save(snap, self.dir)
        c.mirror_max_age_s = 60
        time.sleep(1.1)                                   # a new snapshot id (seconds)
        c.check("lab", "drop_path", {"name": "acme"})
        self.assertNotEqual(state.latest("lab")["snapshot_id"], first)

    def test_the_check_sees_what_changed_on_the_network(self):
        c = self.checker
        self.assertEqual(c.check("lab", "add_path", {"name": "w", "from": "pe-a", "to": "pe-b",
                                                     "bandwidth_mbps": 35})["action"], "propose")
        self.lab.st["services"]["big"] = {"from": "pe-a", "to": "pe-b", "bandwidth_mbps": 40}
        c.snapshot("lab", refresh=True)
        self.assertEqual(c.check("lab", "add_path", {"name": "w2", "from": "pe-a", "to": "pe-b",
                                                     "bandwidth_mbps": 35})["action"], "reject")


class ContractCheck(Lab):
    """check_write through the contract: what an agent's gate holds a controller write to."""

    def setUp(self):
        super().setUp()
        import tempfile
        from netwins.contract import NetwinsTwin
        self.t = NetwinsTwin(home=tempfile.mkdtemp(), engine="fake", source=AgenticSource(self.url))
        self.t.twin = Twin(FakeEngine(), calibrated())
        os.environ["NETWINS_MIRROR_INTERVAL_S"] = "0.3"

    def test_a_supported_write_may_go_and_says_how(self):
        c = self.t.check_write("lab", "provision", {"name": "w", "from": "pe-a", "to": "dc-1", "bandwidth_mbps": 10}, {})
        self.assertEqual((c["action"], c["tool"]), ("propose", "add_path"))
        self.assertIn("lab__add_path", c["send_as"])
        self.assertEqual(c["result"]["decision"]["action"], "propose")
        self.assertEqual(c["change_ran"]["add_demands"][0]["rate_mbps"], 10.0)

    def test_a_rejected_or_unclear_write_says_what_next(self):
        c = self.t.check_write("lab", "add_path", {"name": "b", "from": "pe-a", "to": "pe-b", "bandwidth_mbps": 60}, {})
        self.assertEqual(c["action"], "reject")
        c = self.t.check_write("lab", "add_path", {"name": "v", "from": "pe-a", "to": "pe-b"}, {})
        self.assertEqual(c["action"], "ask_operator")
        self.assertTrue(c["questions"])
        c = self.t.check_write("lab", "add_path", {"name": "v", "from": "pe-a", "to": "pe-b"}, {"rate_mbps": 15})
        self.assertEqual(c["action"], "propose")

    def test_canahs_contract_models_accept_the_check(self):
        try:
            from canah.twins import contract as C
        except ImportError:
            self.skipTest("CANAH is not importable here")
        C.Check.model_validate(self.t.check_write("lab", "set_port", {"dpid": "p-1", "port_no": 2, "admin_state": "down"}, {}))


if __name__ == "__main__":
    unittest.main()
