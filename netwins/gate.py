"""
No write reaches a network the twin covers without the twin's check, or an operator's explicit word
to go ahead without it.

  Checker      runs a check: mirror the controller if its snapshot is stale, translate the write into a
               scenario (changes.py), predict, decide (policy.py). Keeps every check under an id, keyed by
               (controller, tool, arguments), so the write that follows finds it.
  TwinGate     CANAH middleware, on the main agent and every subagent (canah.yaml `middleware`). When a
               controller write is about to run (after the operator approved it):
                 a fresh check with a go-ahead (propose, auto_apply_eligible)   it runs
                 an operator override of that check (twin_override, approved)   it runs, noted
                 no check yet                                                  the check runs now, then as above
                 anything else (reject, ask_operator, gather_more_data,        it does not run; the agent is told
                   not_modelled)                                               why and what to do next

Writes to controllers whose domain has no twin (optical, today) pass untouched, unless
NETWINS_GATE_STRICT=1, which holds them to an override too.

  NETWINS_MIRROR_MAX_AGE_S   re-mirror before a check when the snapshot is older (default 300)
  NETWINS_CHECK_TTL_S        a check is good for this long (default 900)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import changes, mirror, source as src, state
from .twin import Twin, compact

logger = logging.getLogger("netwins.gate")

TWIN_DOMAINS = {"packet", "ip"}
GO = {"propose", "auto_apply_eligible"}

NEXT = {
    "propose": ("The twin supports it. Send the write now, as `send_as` says (through the controller's subagent if "
                "you do not hold its tools); the operator approves it. No override is needed. Then report the twin's "
                "figures with the result."),
    "auto_apply_eligible": ("The twin supports it strongly. Send the write now, as `send_as` says; the operator "
                            "still approves it. No override is needed."),
    "reject": ("Do not send it, and do not send a different change in its place. Tell the operator what the twin "
               "predicts and why it was rejected. You may suggest what would fit (check it in the twin first and "
               "give its result), then ask whether they want it; send nothing until they say so."),
    "gather_more_data": ("Do not send it yet. Tell the operator what is unclear (the reasons) and ask how to "
                         "proceed: more information, a different change, or to go ahead anyway (twin_override)."),
    "ask_operator": ("Do not send it yet. Ask the operator these questions, then call twin_check_write again with "
                     "their answers in `answers`."),
    "not_modelled": ("The twin cannot check this change. Ask the operator whether to go ahead without the twin's "
                     "evidence; only if they say so, call twin_override with this check_id and their reason, then "
                     "send the write."),
    "no_twin": "This controller's domain has no twin; the write goes through the operator's approval alone.",
    "unknown_tool": ("That is not one of this controller's tools. Check again with one of `write_tools` (the "
                     "controller's own name), or a contract verb (provision, teardown, adjust, configure_device). "
                     "Send the write with the same tool and arguments you checked."),
}


def _norm(v: Any) -> Any:
    """Arguments as the controller will read them: "2" and 2 alike, "DOWN" and "down" alike."""
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip()
    try:
        return float(t)
    except ValueError:
        return t.lower()


def _key(controller: str, tool: str, args: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([controller, tool, _norm(args or {})], sort_keys=True,
                                     default=str).encode()).hexdigest()[:20]


def _user() -> str:
    try:
        from langgraph.config import get_config
        c = (get_config() or {}).get("configurable") or {}
        return str(c.get("user_id") or c.get("user") or "")
    except Exception:  # noqa: BLE001 - outside a run
        return ""


class Checker:
    def __init__(self, twin: Twin, source: Any = None, snapshots: Optional[str] = None,
                 mirror_max_age_s: Optional[float] = None, check_ttl_s: Optional[float] = None,
                 interval_s: float = 5.0, runs_dir: Optional[Path] = None):
        self.twin = twin
        self.source = source or src.AgenticSource()
        self.snapshots = snapshots
        self.mirror_max_age_s = float(mirror_max_age_s if mirror_max_age_s is not None
                                      else os.getenv("NETWINS_MIRROR_MAX_AGE_S", "300"))
        self.check_ttl_s = float(check_ttl_s if check_ttl_s is not None else os.getenv("NETWINS_CHECK_TTL_S", "900"))
        self.interval_s = interval_s
        self.runs_dir = runs_dir
        self.checks: Dict[str, Dict[str, Any]] = {}
        self.by_key: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._ctl: Dict[str, Dict[str, Any]] = {}

    # -- controllers
    def controller(self, key: str) -> Dict[str, Any]:
        if key not in self._ctl:
            row = src.controller(self.source, key)
            self._ctl[key] = {"row": row, "tools": self.source.tools(key)}
        return self._ctl[key]

    def is_write(self, key: str, tool: str) -> Optional[bool]:
        try:
            c = self.controller(key)
        except Exception:  # noqa: BLE001 - not a controller this checker knows
            return None
        for t in c["tools"]:
            if t["name"] == tool:
                return bool(t.get("write"))
        return None

    def write_tools(self, key: str) -> Dict[str, Optional[str]]:
        """{the controller's write tool: the contract verb it serves, or None}."""
        c = self.controller(key)
        v = {tool: verb for verb, tool in src.verbs(c["row"], c["tools"]).items()}
        return {t["name"]: v.get(t["name"]) for t in c["tools"] if t.get("write")}

    def resolve_tool(self, key: str, asked: str) -> Optional[str]:
        """A contract verb (provision) or the contract's tool name (create_service), as this controller's tool."""
        c = self.controller(key)
        v = src.verbs(c["row"], c["tools"])
        if asked in v:
            return v[asked]
        verb = next((vb for vb, name in src.WRITES.items() if name == asked), None)
        return v.get(verb) if verb else None

    def covered(self, key: str) -> bool:
        return str(self.controller(key)["row"].get("domain") or "").lower() in TWIN_DOMAINS

    # -- snapshots
    def folder(self) -> Path:
        return Path(self.snapshots or os.getenv("NETWINS_SNAPSHOTS") or state.folder())

    def snapshot(self, key: str, refresh: bool = False) -> Dict[str, Any]:
        snap = state.latest(key)
        if refresh or snap is None or state.age_s(snap) > self.mirror_max_age_s:
            snap = mirror.mirror(self.source, key, interval_s=self.interval_s)
            mirror.save(snap, self.folder())
        return snap

    # -- checks
    def check(self, controller: str, tool: str, arguments: Dict[str, Any],
              answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        arguments = dict(arguments or {})
        cid = uuid.uuid4().hex[:10]
        rec: Dict[str, Any] = {"check_id": cid, "controller": controller, "tool": tool, "arguments": arguments,
                               "at": time.time(), "by": _user()}
        try:
            c = self.controller(controller)
        except Exception as e:  # noqa: BLE001
            rec.update(action="not_modelled", reasons=[f"no controller {controller!r} in the agentic MCP: {e}"])
            return self._keep(rec)
        known = {t["name"] for t in c["tools"]}
        if tool not in known:
            mapped = self.resolve_tool(controller, tool)
            if mapped is None:
                rec.update(action="unknown_tool", reasons=[f"{controller} has no tool {tool!r}"],
                           write_tools=self.write_tools(controller))
                return self._keep(rec)
            rec["asked_as"], tool = tool, mapped
            rec["tool"] = tool
        if not self.covered(controller):
            rec.update(action="no_twin", reasons=[f"{controller} is a {c['row'].get('domain')} controller: no twin "
                                                  f"for that domain yet"])
            return self._keep(rec)
        verb = src.tool_verb(c["row"], c["tools"], tool)
        try:
            snap = self.snapshot(controller)
        except Exception as e:  # noqa: BLE001
            rec.update(action="gather_more_data", reasons=[f"the twin could not mirror {controller}: "
                                                           f"{type(e).__name__}: {e}"])
            return self._keep(rec)
        rec["snapshot_id"], rec["verb"] = snap["snapshot_id"], verb
        t = changes.translate(snap, verb, tool, arguments, answers)
        if not t["modelled"]:
            if t.get("questions"):
                rec.update(action="ask_operator", questions=t["questions"], reasons=t["questions"])
            else:
                rec.update(action="not_modelled", reasons=[t.get("not_modelled", "not modelled")])
            return self._keep(rec)
        cap, why = mirror.trust_caps(snap)
        try:
            env = self.twin.predict(t["scenario"], snapshot_age_s=state.age_s(snap), trust_cap=cap, cap_why=why)
        except Exception as e:  # noqa: BLE001 - a twin that fails says so and holds the write; it never crashes the agent
            logger.exception("twin run failed for %s %s", controller, tool)
            rec.update(action="gather_more_data", reasons=[f"the twin failed to run this change: "
                                                           f"{type(e).__name__}: {str(e)[:300]}"],
                       change=t["scenario"]["change"])
            return self._keep(rec)
        if not env.get("ok"):
            rec.update(action="gather_more_data", reasons=["the scenario built from this write did not validate: "
                                                           + "; ".join(env.get("problems", []))[:600]])
            return self._keep(rec)
        if self.runs_dir:
            Path(self.runs_dir).mkdir(parents=True, exist_ok=True)
            (Path(self.runs_dir) / f"{env['run_id']}.json").write_text(json.dumps(env, default=str))
        rec.update(action=env["decision"]["action"], reasons=env["decision"]["reasons"], twin=compact(env),
                   change=t["scenario"]["change"], assumptions=t.get("assumptions", []),
                   snapshot_coverage=snap.get("coverage"), snapshot_assumed=[p for p in snap.get("provenance", [])
                                                                             if p.startswith("assumed")])
        return self._keep(rec)

    def _keep(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        rec["next"] = NEXT.get(rec["action"], "")
        with self._lock:
            self.checks[rec["check_id"]] = rec
            self.by_key[_key(rec["controller"], rec["tool"], rec["arguments"])] = rec["check_id"]
        logger.info("twin check %s %s %s: %s", rec["check_id"], rec["controller"], rec["tool"], rec["action"])
        return rec

    def override(self, check_id: str, reason: str, by: str = "") -> Dict[str, Any]:
        with self._lock:
            rec = self.checks.get(check_id)
            if rec is None:
                return {"ok": False, "error": f"no twin check {check_id!r}"}
            if not reason.strip():
                return {"ok": False, "error": "an override needs the operator's reason"}
            rec["override"] = {"by": by or _user(), "reason": reason, "at": time.time()}
        return {"ok": True, "check_id": check_id, "override": rec["override"],
                "next": "The operator's override is recorded. Send the write exactly as checked."}

    def verdict(self, controller: str, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """The check a write is held to: the latest one for exactly this call, if still fresh; else a new one."""
        fresh = lambda r: r is not None and time.time() - r["at"] <= self.check_ttl_s   # noqa: E731
        with self._lock:
            cid = self.by_key.get(_key(controller, tool, args))
            rec = self.checks.get(cid) if cid else None
        if fresh(rec):
            return rec
        # The same change, written differently (another argument order, an extra label): the check of the
        # change it makes in the twin holds for it, override included.
        try:
            c = self.controller(controller)
            snap = state.latest(controller)
            if snap is not None:
                t = changes.translate(snap, src.tool_verb(c["row"], c["tools"], tool), tool, args)
                if t["modelled"]:
                    with self._lock:
                        same = [r for r in self.checks.values() if r["controller"] == controller and r["tool"] == tool
                                and r.get("change") == t["scenario"]["change"] and fresh(r)]
                    if same:
                        return max(same, key=lambda r: (bool(r.get("override")), r["at"]))
        except Exception:  # noqa: BLE001 - fall through to a new check
            pass
        return self.check(controller, tool, args)


CHECKER: Optional[Checker] = None      # set by the netwins provider; the gate reads it


def summary(rec: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: rec[k] for k in ("check_id", "controller", "tool", "asked_as", "verb", "action", "reasons", "next",
                               "questions", "write_tools", "snapshot_id", "change", "override") if rec.get(k) is not None}
    if rec.get("action") in GO or rec.get("asked_as"):
        out["send_as"] = f"{rec['controller']}__{rec['tool']} with exactly these arguments: {json.dumps(rec['arguments'])}"
    tw = rec.get("twin") or {}
    if tw:
        out["twin"] = {k: tw.get(k) for k in ("p_meets_requirement", "p_meets_baseline", "trust_tier", "calibration",
                                              "figures (changed / baseline / effect / calibrated_90)", "members",
                                              "not_modelled", "run_id")}
    if rec.get("snapshot_assumed"):
        out["snapshot_assumed"] = rec["snapshot_assumed"]
    return out


try:
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import ToolMessage
except ImportError:  # netwins without CANAH's dependencies: the gate is not used there
    AgentMiddleware = object          # type: ignore
    ToolMessage = None                # type: ignore


class TwinGate(AgentMiddleware):
    """canah.yaml:  middleware: [netwins.gate:TwinGate]"""

    def __init__(self, strict: Optional[bool] = None):
        super().__init__()
        self.strict = (os.getenv("NETWINS_GATE_STRICT", "") == "1") if strict is None else strict

    def _hold(self, request):
        """(a message that stops the write, or None; the check that let it through, or None)."""
        name = str(request.tool_call.get("name") or "")
        if "__" not in name or CHECKER is None:
            return None, None
        controller, tool = name.split("__", 1)
        if CHECKER.is_write(controller, tool) is not True:
            return None, None
        args = request.tool_call.get("args") or {}
        rec = CHECKER.verdict(controller, tool, args)
        if rec["action"] in GO or rec.get("override"):
            return None, rec
        if rec["action"] == "no_twin" and not self.strict:
            return None, None
        body = summary(rec)
        return ToolMessage(tool_call_id=request.tool_call["id"], name=name, status="error", content=(
            f"Not sent: the digital twin holds this write ({rec['action']}).\n" + json.dumps(body, indent=1, default=str))), None

    def _after(self, rec, result):
        if rec is None or ToolMessage is None or not isinstance(result, ToolMessage):
            return result
        note = (f"\n\n[Checked in the digital twin first: check {rec['check_id']}, {rec['action']}"
                + (f", overridden by {rec['override']['by']}: {rec['override']['reason']}" if rec.get("override") else "")
                + (f", p={rec['twin']['p_meets_requirement']['p']:.3f}, tier {rec['twin']['trust_tier']}"
                   if rec.get("twin") else "") + ".]")
        text = result.content if isinstance(result.content, str) else str(result.content)
        return result.model_copy(update={"content": text + note})

    def wrap_tool_call(self, request, handler: Callable):
        held, rec = self._hold(request)
        return held if held is not None else self._after(rec, handler(request))

    async def awrap_tool_call(self, request, handler: Callable):
        import asyncio
        held, rec = await asyncio.to_thread(self._hold, request)
        return held if held is not None else self._after(rec, await handler(request))
