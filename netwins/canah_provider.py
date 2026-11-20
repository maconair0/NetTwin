"""
netwins as a CANAH provider: the twin's tools for the agent. All reads: the twin predicts, it never
changes the network (a change the prediction supports is made through the controllers' own tools, with
CANAH's approval).

  twin_domains       the domains that have a twin and a snapshot
  twin_inspect       a domain's latest snapshot, summarised (nodes, links, demands, age)
  twin_subnetwork    the slice around some nodes: the topology and demands a scenario needs
  twin_schema        the scenario format, with a worked example
  twin_validate      check a scenario without running it (schema, then semantics)
  twin_predict       the whole pipeline: ensemble (paired with the baseline when there is a change),
                     calibration, envelope, policy decision

The live loop (gate.py): the twin mirrors a controller through the agentic MCP, and a controller write
is tried in the twin before it is sent.
  twin_mirror        read a controller's network into a fresh snapshot: what was measured, what assumed
  twin_check_write   a proposed write (controller, tool, arguments) run in the twin: the decision and what
                     to do next (send it, do not, or ask the operator)
  twin_override      (a write: it waits for an operator's approval) go ahead with a write the twin could
                     not support, with the operator's reason
And `netwins.gate:TwinGate` in canah.yaml `middleware` holds every controller write to its check.

In canah.yaml:
  providers: [canah.network:provider, netwins.canah_provider:provider]
  middleware: [netwins.gate:TwinGate]
  skills_dirs: [..., /path/to/netwins/skills]

  NETWINS_ENGINE   docker (default) | local:<binary> | fake (tests only)
  NETWINS_HOME     where the calibration store lives (default ./netwins_data)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, field_validator

from . import gate, mirror, state
from .calibration import Calibration
from .runner import DockerEngine, FakeEngine, LocalEngine
from .schema import EXAMPLE, json_schema
from .twin import Twin, compact



class ScenarioArg(BaseModel):
    """The scenario as a JSON object. A JSON string is accepted too: models send either."""
    scenario: Dict[str, Any] = Field(description="the scenario, a JSON object in the format twin_schema shows")

    @field_validator("scenario", mode="before")
    @classmethod
    def _text(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except ValueError as e:
                raise ValueError(f"the scenario is not valid JSON: {e}") from None
        return v


def _json_obj(v):
    if isinstance(v, str):
        try:
            return json.loads(v) if v.strip() else {}
        except ValueError as e:
            raise ValueError(f"not valid JSON: {e}") from None
    return v


class CheckArg(BaseModel):
    controller: str = Field(description="the controller's key, as network_controllers names it (onos, ryu, ...)")
    tool: str = Field(description=("the controller's own write tool, without the '<controller>__' prefix (twin_mirror "
                                   "lists them), or a contract verb: provision, teardown, adjust, configure_device"))
    arguments: Dict[str, Any] = Field(default_factory=dict, description="exactly the arguments the write will be sent with")
    answers: Dict[str, Any] = Field(default_factory=dict, description=(
        "what the operator answered to the twin's questions: src, dst, rate_mbps, service, link, requirements"))

    @field_validator("arguments", "answers", mode="before")
    @classmethod
    def _text(cls, v):
        return _json_obj(v)


def _engine():
    spec = os.getenv("NETWINS_ENGINE", "docker")
    if spec == "fake":
        return FakeEngine()
    if spec.startswith("local:"):
        return LocalEngine(spec.split(":", 1)[1])
    return DockerEngine()


class NetwinsProvider:
    def __init__(self, twin: Twin = None, home: str = "", source: Any = None, checker: gate.Checker = None):
        home = home or os.getenv("NETWINS_HOME", "netwins_data")
        Path(home).mkdir(parents=True, exist_ok=True)
        self.home = Path(home)
        self.twin = twin or Twin(_engine(), Calibration(self.home / "calibration.sqlite"))
        self.checker = checker or gate.Checker(self.twin, source, runs_dir=self.home / "runs")
        gate.CHECKER = self.checker           # the gate middleware holds writes to this checker's verdicts
        self._tools = self._make()

    def _make(self) -> List[BaseTool]:
        twin = self.twin
        runs = self.home / "runs"
        runs.mkdir(exist_ok=True)

        def twin_domains() -> str:
            """The network domains that have a digital twin, each with its latest snapshot id. Other twin tools
            take the domain (or the snapshot id)."""
            return json.dumps([{"domain": d, "snapshot_id": sid} for d, sid in state.domains().items()])

        def twin_inspect(domain: str) -> str:
            """A domain's latest snapshot as the twin sees it: nodes, links (rate, delay), demands, its age."""
            return json.dumps(state.inspect(domain))

        def twin_subnetwork(domain: str, anchors: str, hops: int = 2) -> str:
            """The slice of a domain's snapshot around some nodes or links (comma-separated ids; empty for the
            whole network), `hops` links out: the topology and demands to copy into a scenario, with the
            snapshot_id to put in it. Build every scenario from this, never from memory."""
            return json.dumps(state.subnetwork(domain, [a.strip() for a in anchors.split(",") if a.strip()], hops))

        def twin_schema(domain: str = "ip") -> str:
            """The scenario format for a domain's twin (JSON Schema), with a worked example. A what-if goes in
            `change` (set values, or events); the twin then also runs the unchanged network for comparison."""
            return json.dumps({"example": EXAMPLE, "json_schema": json_schema()})

        def twin_validate(scenario: Dict[str, Any]) -> str:
            """Check a scenario (JSON) without running it: the schema, then the semantics (references, ranges,
            event times). Returns the problems to fix, if any."""
            return json.dumps(twin.validate(scenario))

        def twin_predict(scenario: Dict[str, Any]) -> str:
            """Run a scenario (JSON) through the digital twin: an ensemble of ns-3 simulations, paired with the
            unchanged network when there is a `change`, calibrated, and decided by the policy. Returns the
            decision (reject / gather_more_data / propose / auto_apply_eligible; met / not_met when there is no change) with its reasons, the
            probability that the requirements are met, the figures behind each requirement, the trust tier,
            and what the twin does not model. Report these as given; the decision is the policy's, not yours."""
            snap = None
            try:
                sid = scenario.get("snapshot_id") or ""
                snap = state.get(sid) if sid else None
            except (ValueError, AttributeError):
                pass
            cap, why = mirror.trust_caps(snap) if snap else (None, [])
            try:
                env = twin.predict(scenario, snapshot_age_s=state.age_s(snap) if snap else None, trust_cap=cap,
                                   cap_why=why)
            except Exception as e:  # noqa: BLE001 - told to the model, never a crash
                return f"Error: the twin failed to run this scenario: {type(e).__name__}: {str(e)[:400]}"
            if env.get("ok"):
                (runs / f"{env['run_id']}.json").write_text(json.dumps(env, default=str))
            return json.dumps(compact(env), default=str)

        checker = self.checker

        def twin_mirror(controller: str) -> str:
            """Read a controller's network (through the agentic MCP) into a fresh twin snapshot: devices, links,
            hosts, services and the traffic its counters show now. Returns what the twin now holds and which of
            it was measured or assumed. Do this when you start working on a controller's network; checks
            re-mirror on their own when the snapshot gets old."""
            try:
                snap = checker.snapshot(controller, refresh=True)
            except Exception as e:  # noqa: BLE001 - told to the model
                return f"Error: could not mirror {controller}: {type(e).__name__}: {e}"
            cap, why = mirror.trust_caps(snap)
            t = snap["topology"]
            return json.dumps({"snapshot_id": snap["snapshot_id"], "network": snap["network"],
                               "nodes": len(t["nodes"]), "links": len(t["links"]),
                               "demands": [d["id"] for d in snap["traffic"]["demands"]][:60],
                               "coverage": snap["coverage"], "provenance": snap["provenance"][:20],
                               "best_trust_tier": cap or "A", "why": why,
                               "write_tools (tool: contract verb)": checker.write_tools(controller)})

        def twin_check_write(controller: str, tool: str, arguments: Dict[str, Any] = None,
                             answers: Dict[str, Any] = None) -> str:
            """Try a controller write in the digital twin BEFORE sending it: the twin of the live network runs
            with and without the change, and its policy decides. Call it with exactly the arguments you will
            send. Returns the check's id, the decision (propose: send it for the operator's approval; reject:
            do not; ask_operator: ask the questions it lists, then check again with `answers`; gather_more_data
            or not_modelled: ask the operator how to proceed), the reasons, the twin's figures, and `next`:
            what to do now. A write sent without a passing check is held back."""
            rec = checker.check(controller, tool, arguments or {}, answers or {})
            return json.dumps(gate.summary(rec), default=str)

        def twin_override(check_id: str, reason: str) -> str:
            """Go ahead with a write the twin did not support (rejected, unclear or not modelled) because the
            OPERATOR said so. Only when the operator has said it, in their words: `reason` is theirs. It waits
            for the operator's approval like any change; once approved, the write of that check may be sent."""
            return json.dumps(checker.override(check_id, reason, gate._user()))

        plain = [StructuredTool.from_function(f) for f in (twin_domains, twin_inspect, twin_subnetwork, twin_schema,
                                                            twin_mirror, twin_override)]
        takes = [StructuredTool.from_function(f, args_schema=ScenarioArg) for f in (twin_validate, twin_predict)]
        takes.append(StructuredTool.from_function(twin_check_write, args_schema=CheckArg))
        return plain + takes

    def tools(self) -> List[BaseTool]:
        return list(self._tools)

    def writes(self) -> List[str]:
        return ["twin_override"]           # an override is the operator's to give: it pauses for approval

    def shared_tools(self) -> List[str]:
        # The controllers' subagents make the writes, so they check them too.
        return ["twin_check_write", "twin_override", "twin_mirror"]

    def instructions(self) -> str:
        return ("Digital twins (netwins). Every packet network a controller holds has a twin: a copy of the live "
                "network, mirrored through the agentic MCP, that runs a change before the network does.\n"
                "- When you start on a controller's network, twin_mirror it: you then know what the twin holds and "
                "what it had to assume.\n"
                "- BEFORE any write to a controller (create, delete or change a service, configure a device), call "
                "twin_check_write with the controller, the tool and exactly the arguments you will send. Do what "
                "its `next` says: send the write only on propose; on reject, do not send it and tell the operator "
                "why; on ask_operator, gather_more_data or not_modelled, ask the operator and wait for the answer. "
                "A write without a passing check is held back by the gate.\n"
                "- When the operator answers the twin's questions, check again with their answers in `answers`. "
                "When they say to go ahead regardless, twin_override with their reason, then send the write.\n"
                "- Report the twin's decision, its probability, the figures behind it, the trust tier and what it "
                "assumed, as given. The decision is the twin's policy's, not yours.\n"
                "- For what-if questions with no write (a failure, an upgrade), build a scenario from "
                "twin_subnetwork and run twin_predict.")


def provider() -> NetwinsProvider:
    return NetwinsProvider()
