"""
Where a twin's live state comes from: the agentic MCP, the one service that holds every controller
behind a common contract (agentic-mcp). netwins talks to it over HTTP, as CANAH does, with the same
settings, so inside a CANAH process it sees the same controllers:

  AGENTIC_MCP_URL     default http://127.0.0.1:8400
  AGENTIC_MCP_TOKEN   bearer token, when the service requires one

Only reads go through here. netwins never sends a write: a change goes to the network through CANAH's
controller tools, after the twin's check and an operator's approval.

The contract names verbs (read_links, provision, ...); each controller serves a verb with a tool of its
own (ONOS `get_links`, Ryu `ryu_get_topology`). Onboarding records which, under the controller's
`conformance` ("tool get_links"); `verbs()` reads that back, and falls back on the contract's own tool
names (list_links, ...) for a controller that serves them directly.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

import requests

# The contract verbs the twin uses, and the contract's own tool name for each.
READS = {"read_devices": "list_devices", "read_ports": "list_ports", "read_links": "list_links",
         "read_hosts": "list_hosts", "read_services": "list_services", "read_statistics": "get_statistics"}
WRITES = {"provision": "create_service", "teardown": "delete_service", "adjust": "update_service",
          "reroute": "reroute_service", "configure_device": "configure_device"}


class AgenticSource:
    def __init__(self, url: str = "", token: str = "", timeout: float = 60.0):
        self.url = (url or os.getenv("AGENTIC_MCP_URL") or "http://127.0.0.1:8400").rstrip("/")
        self.token = token if token else os.getenv("AGENTIC_MCP_TOKEN", "")
        self.timeout = timeout
        self.http = requests.Session()

    def _h(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def controllers(self) -> List[Dict[str, Any]]:
        r = self.http.get(self.url + "/controllers", headers=self._h(), timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("controllers", [])

    def tools(self, key: str) -> List[Dict[str, Any]]:
        r = self.http.get(f"{self.url}/controllers/{key}/tools", headers=self._h(), timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("tools", [])

    def call(self, key: str, tool: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """A read. Returns the controller's text; raises on an error, so a mirror never mistakes an
        error message for an empty network."""
        r = self.http.post(f"{self.url}/controllers/{key}/call", headers=self._h(), timeout=self.timeout,
                           json={"tool": tool, "arguments": arguments or {}, "approved_by": ""})
        if r.status_code >= 400:
            raise RuntimeError(f"{key} {tool}: HTTP {r.status_code}: {r.text[:200]}")
        got = r.json()
        text = str(got.get("text", ""))
        if got.get("is_error") or text.lower().startswith("error"):
            raise RuntimeError(f"{key} {tool}: {text[:300]}")
        return text


def controller(source: Any, key: str) -> Dict[str, Any]:
    for c in source.controllers():
        if c.get("key") == key:
            return c
    raise KeyError(f"the agentic MCP holds no controller {key!r}")


def verbs(row: Dict[str, Any], tools: List[Dict[str, Any]]) -> Dict[str, str]:
    """{verb: the controller's tool} for the verbs the twin uses."""
    names = {t["name"] for t in tools}
    out: Dict[str, str] = {}
    for d in ((row.get("conformance") or {}).get("domains") or []):
        for verb, how in (d.get("features") or {}).items():
            m = re.match(r"tool (\S+)", str(how))
            if m and m.group(1) in names:
                out.setdefault(verb, m.group(1))
    for verb, tool in {**READS, **WRITES}.items():
        if verb not in out and tool in names:
            out[verb] = tool
    return out


def tool_verb(row: Dict[str, Any], tools: List[Dict[str, Any]], tool: str) -> Optional[str]:
    """Which contract verb a controller's tool serves, if any."""
    for verb, name in verbs(row, tools).items():
        if name == tool:
            return verb
    return None


def required_args(tools: List[Dict[str, Any]], tool: str) -> List[str]:
    for t in tools:
        if t["name"] == tool:
            return list((t.get("schema") or {}).get("required") or [])
    return []


def parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text
