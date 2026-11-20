"""netwins: schema, ensemble, aggregation, calibration, policy, pipeline. Fake engine except where marked."""
import json
import os
import subprocess
import time
import unittest
from pathlib import Path

import numpy as np

from netwins import aggregate
from netwins.calibration import Calibration
from netwins.policy import Policy
from netwins.runner import DockerEngine, FakeEngine, members, run_batch
from netwins.schema import baseline_and_changed, parse, problems
from netwins.twin import Twin

HERE = Path(__file__).resolve().parent.parent
EXAMPLE = json.loads((HERE / "examples" / "acme-L12-failure.json").read_text())


def scenario(**over):
    s = json.loads(json.dumps(EXAMPLE))
    s.update(over)
    return s


class Schema(unittest.TestCase):
    def test_the_example_is_valid(self):
        self.assertEqual(problems(parse(EXAMPLE)), [])

    def test_unknown_version_is_refused_at_the_schema_stage(self):
        got = Twin(FakeEngine()).validate(scenario(schema_version="9.9"))
        self.assertEqual((got["ok"], got["stage"]), (False, "schema"))

    def test_semantic_problems_are_listed(self):
        bad = scenario()
        bad["topology"]["links"].append({"id": "L-x", "a": "pe-a", "z": "nowhere", "rate_mbps": 10})
        bad["change"]["events"][0]["target"] = "L-zz"
        bad["requirements"][0]["demand"] = "nobody"
        bad["uncertain_params"][0]["path"] = "links.L-12.colour"
        bad["traffic"]["demands"][0]["rate_mbps"] = 1e9
        got = problems(parse(bad))
        for needle in ("no node 'nowhere'", "no link 'L-zz'", "no demand 'nobody'", "cannot be set or sampled",
                       "outside the validated range"):
            self.assertTrue(any(needle in p for p in got), (needle, got))

    def test_the_change_is_applied_only_to_the_changed_side(self):
        sc = parse(scenario(change={"set": [{"path": "links.L-13.rate_mbps", "value": 80}], "events": []}))
        base, changed = baseline_and_changed(sc)
        rate = lambda d: next(l for l in d["topology"]["links"] if l["id"] == "L-13")["rate_mbps"]
        self.assertEqual((rate(base), rate(changed)), (40, 80))


class AgentFacing(unittest.TestCase):
    def test_the_worked_example_the_agent_is_shown_is_valid(self):
        from netwins.schema import EXAMPLE
        self.assertEqual(problems(parse(EXAMPLE)), [])

    def test_the_skill_has_valid_front_matter(self):
        import yaml
        head = (HERE / "skills" / "ip-twin" / "SKILL.md").read_text().split("---")[1]
        self.assertEqual(yaml.safe_load(head)["name"], "ip-twin")

    def test_a_domain_or_a_snapshot_id_both_find_the_snapshot(self):
        from netwins import state
        self.assertEqual(state.inspect("ip")["snapshot_id"], state.inspect("ip-lab-001")["snapshot_id"])
        self.assertIn("available", state.inspect("nowhere"))

    def test_a_link_as_an_anchor_stands_for_its_ends(self):
        from netwins import state
        got = state.subnetwork("ip", ["L-12"], 0)
        self.assertEqual(sorted(n["id"] for n in got["topology"]["nodes"]), ["p-1", "p-2"])
        whole = state.subnetwork("ip", [], 0)
        self.assertEqual(len(whole["topology"]["links"]), 6)


class Ensemble(unittest.TestCase):
    def test_a_pair_shares_its_seed_and_sampled_inputs(self):
        sc = parse(EXAMPLE)
        for seed, base, changed in members(sc, 0, 5):
            g = lambda d: next(x for x in d["traffic"]["demands"] if x["id"] == "globex")["rate_mbps"]
            self.assertEqual(g(base), g(changed))
        draws = [next(x for x in m[1]["traffic"]["demands"] if x["id"] == "globex")["rate_mbps"]
                 for m in members(sc, 0, 30)]
        self.assertAlmostEqual(float(np.mean(draws)), 20, delta=1.5)

    def test_members_are_reproducible(self):
        sc = parse(EXAMPLE)
        self.assertEqual(members(sc, 3, 2), members(sc, 3, 2))

    def test_the_effect_is_paired(self):
        res = run_batch(FakeEngine(noise=0.0), members(parse(EXAMPLE), 0, 4), paired=True)
        s = aggregate.summarise(res)
        self.assertLess(s["acme"]["loss_ratio"]["baseline"]["mean"], 0.001)
        self.assertGreater(s["acme"]["loss_ratio"]["effect"]["mean"], 0.1)

    def test_wilson(self):
        lo, hi = aggregate.wilson(20, 20)
        self.assertGreater(lo, 0.8)
        self.assertEqual(hi, 1.0)


class Pipeline(unittest.TestCase):
    def test_a_clear_failure_is_rejected_even_uncalibrated(self):
        env = Twin(FakeEngine()).predict(EXAMPLE)
        self.assertEqual(env["decision"]["action"], "reject")
        self.assertEqual(env["p_meets_requirement"]["p"], 0.0)
        self.assertEqual(env["p_meets_baseline"]["p"], 1.0)
        self.assertEqual(env["trust_tier"], "C")

    def test_an_uncalibrated_twin_never_lets_a_change_go_ahead(self):
        ok = scenario(change={"set": [{"path": "links.L-13.rate_mbps", "value": 100},
                                      {"path": "links.L-3b.rate_mbps", "value": 100}],
                              "events": [{"t": 10, "type": "link_down", "target": "L-12"}]})
        env = Twin(FakeEngine()).predict(ok)
        self.assertGreater(env["p_meets_requirement"]["p"], 0.9)
        self.assertEqual(env["decision"]["action"], "gather_more_data")

    def test_a_calibrated_twin_proposes_and_says_so(self):
        cal = Calibration()
        for _ in range(40):
            cal.add("ip", "loss_ratio", {"p5": 0.0, "p50": 0.0, "p95": 0.0}, float(np.random.uniform(-0.001, 0.001)),
                    "testbed")
        ok = scenario(change={"set": [{"path": "links.L-13.rate_mbps", "value": 100},
                                      {"path": "links.L-3b.rate_mbps", "value": 100}],
                              "events": [{"t": 10, "type": "link_down", "target": "L-12"}]})
        env = Twin(FakeEngine(), cal).predict(ok)
        self.assertEqual(env["trust_tier"], "A")
        self.assertEqual(env["decision"]["action"], "propose")
        self.assertIn("model_error", env["uncertainty_sources"])
        self.assertIsNotNone(env["prediction"]["acme"]["loss_ratio"]["calibrated_90"])

    def test_problems_come_back_instead_of_a_run(self):
        engine = FakeEngine()
        env = Twin(engine).predict(scenario(duration_s=5000))
        self.assertFalse(env["ok"])
        self.assertEqual(engine.calls, 0)

    def test_identical_requests_are_cached(self):
        engine = FakeEngine()
        t = Twin(engine)
        t.predict(EXAMPLE)
        calls = engine.calls
        self.assertTrue(t.predict(EXAMPLE)["cached"])
        self.assertEqual(engine.calls, calls)

    def test_an_undecided_ensemble_grows_to_its_cap(self):
        # A requirement at the edge of the fake engine's noise: p near 0.9, its interval straddling it.
        edge = scenario(change=None, requirements=[{"demand": "acme", "kpi": "throughput_mbps", "op": ">=",
                                                    "value": 29.25}],
                        seed_policy={"runs": 10, "max_runs": 40, "batch": 10, "adaptive": True, "base_seed": 1})
        env = Twin(FakeEngine(noise=0.02)).predict(edge)
        self.assertEqual(env["members"], 40)


class Calibrating(unittest.TestCase):
    def test_conformal_intervals_cover_as_promised(self):
        rng = np.random.default_rng(3)
        cal = Calibration()
        for _ in range(200):
            cal.add("ip", "delay_ms_mean", {"p5": 9.9, "p50": 10.0, "p95": 10.1}, 10 + rng.normal(0, 2), "synthetic")
        lo, hi = cal.interval("ip", "delay_ms_mean", 10.0)
        fresh = 10 + rng.normal(0, 2, 5000)
        coverage = float(np.mean((fresh >= lo) & (fresh <= hi)))
        self.assertGreater(coverage, 0.87)
        st = cal.status("ip", "delay_ms_mean")
        self.assertLess(st["coverage_raw_p5_p95"], 0.2)          # the raw ensemble is overconfident
        self.assertGreater(st["coverage_conformal_90"], 0.85)    # the conformal interval is not
        self.assertEqual(st["trust_tier"], "B")                  # synthetic: never A

    def test_tiers(self):
        cal = Calibration()
        self.assertEqual(cal.status("ip", "loss_ratio")["trust_tier"], "C")
        for i in range(35):
            cal.add("ip", "loss_ratio", {"p5": 0, "p50": 0.01, "p95": 0.02}, 0.01 + (i % 5) * 0.001, "testbed")
        self.assertEqual(cal.status("ip", "loss_ratio")["trust_tier"], "A")
        old = Calibration()
        for i in range(35):
            old.add("ip", "loss_ratio", {"p5": 0, "p50": 0.01, "p95": 0.02}, 0.01, "testbed", at=time.time() - 90 * 86400)
        self.assertEqual(old.status("ip", "loss_ratio")["trust_tier"], "B")   # not recent: not A


class Policies(unittest.TestCase):
    def env(self, p, interval, tier="A", age=10):
        return {"p_meets_requirement": {"p": p, "interval": interval}, "trust_tier": tier, "snapshot_age_s": age}

    def test_the_table(self):
        pol = Policy()
        ch = {"set": [], "events": [{"t": 1, "type": "link_down", "target": "L1"}]}
        self.assertEqual(pol.decide(self.env(0.95, [0.91, 0.98]), ch)["action"], "propose")
        self.assertEqual(pol.decide(self.env(0.5, [0.3, 0.7]), ch)["action"], "reject")
        self.assertEqual(pol.decide(self.env(0.9, [0.8, 0.96]), ch)["action"], "gather_more_data")
        self.assertEqual(pol.decide(self.env(0.95, [0.91, 0.98], age=99999), ch)["action"], "reject")
        self.assertEqual(pol.decide(self.env(0.95, [0.91, 0.98], tier="C"), ch)["action"], "gather_more_data")

    def test_a_question_without_a_change_is_met_or_not_never_proposed(self):
        pol = Policy()
        self.assertEqual(pol.decide(self.env(0.95, [0.91, 0.98]), None)["action"], "met")
        self.assertEqual(pol.decide(self.env(0.2, [0.1, 0.4]), None)["action"], "not_met")
        self.assertEqual(pol.decide(self.env(0.95, [0.91, 0.98], age=99999), None)["action"], "gather_more_data")
        self.assertEqual(pol.decide(self.env(0.95, [0.91, 0.98], tier="C"), None)["action"], "gather_more_data")
        self.assertEqual(pol.decide(self.env(0.95, [0.91, 0.98]), {"set": [], "events": []})["action"], "propose")

    def test_auto_apply_needs_everything(self):
        pol = Policy(auto_apply_enabled=True)
        change = {"set": [{"path": "links.L1.rate_mbps", "value": 150}]}
        self.assertEqual(pol.decide(self.env(1.0, [0.995, 1.0]), change, {"links.L1.rate_mbps": 100})["action"],
                         "auto_apply_eligible")
        big = {"set": [{"path": "links.L1.rate_mbps", "value": 1000}]}
        self.assertEqual(pol.decide(self.env(1.0, [0.995, 1.0]), big, {"links.L1.rate_mbps": 100})["action"], "propose")
        self.assertEqual(Policy().decide(self.env(1.0, [0.995, 1.0]), change, {"links.L1.rate_mbps": 100})["action"],
                         "propose")                                          # off by default


try:
    import langchain_core  # noqa: F401 - only where CANAH's dependencies are installed
    LANGCHAIN = True
except ImportError:
    LANGCHAIN = False


@unittest.skipUnless(LANGCHAIN, "langchain is not installed (run with CANAH's Python)")
class Provider(unittest.TestCase):
    def test_the_scenario_goes_in_as_an_object_or_as_text(self):
        import tempfile
        from netwins.canah_provider import NetwinsProvider
        from netwins.schema import EXAMPLE
        p = NetwinsProvider(Twin(FakeEngine()), home=tempfile.mkdtemp())
        tools = {x.name: x for x in p.tools()}
        self.assertEqual(p.writes(), ["twin_override"])          # an override waits for an operator
        for form in (EXAMPLE, json.dumps(EXAMPLE)):
            self.assertTrue(json.loads(tools["twin_validate"].invoke({"scenario": form}))["ok"])
        got = json.loads(tools["twin_predict"].invoke({"scenario": EXAMPLE}))
        self.assertIn(got["decision"]["action"], ("reject", "gather_more_data", "propose"))


DOCKER = subprocess.run(["docker", "--context", "default", "image", "inspect", "netwins-ns3:3.43"],
                        capture_output=True).returncode == 0


@unittest.skipUnless(DOCKER, "the netwins-ns3 image is not built")
class RealEngine(unittest.TestCase):
    def test_a_link_failure_reroutes_and_the_detour_is_congested(self):
        sc = parse(scenario(seed_policy={"runs": 2, "max_runs": 2, "adaptive": False, "base_seed": 1}))
        res = run_batch(DockerEngine(), members(sc, 0, 2), paired=True)
        s = aggregate.summarise(res)
        self.assertLess(s["acme"]["loss_ratio"]["baseline"]["mean"], 0.01)
        self.assertGreater(s["acme"]["loss_ratio"]["changed"]["mean"], 0.1)
        self.assertGreater(s["acme"]["delay_ms_mean"]["changed"]["mean"], s["acme"]["delay_ms_mean"]["baseline"]["mean"])


if __name__ == "__main__":
    unittest.main()
