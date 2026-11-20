"""
netwins over HTTP: the canah.twin/1 contract's four routes, for an agent in another process or on another
machine (CANAH registers it as an HTTP twin), or for anything that speaks HTTP and JSON.

  GET  /twin                 describe()
  POST /twin/validate        {"scenario": {...}}                                        -> {"ok", "problems"}
  POST /twin/predict         {"scenario": {...}}                                        -> the result
  POST /twin/check_write     {"controller", "tool", "arguments", "answers"}             -> the check
  POST /twin/extra           {"name", "arguments"}: snapshots, inspect, subnetwork, mirror
  GET  /health

  python -m netwins serve [--host 127.0.0.1] [--port 8420]

With NETWINS_TOKEN set, every request but /health needs "Authorization: Bearer <token>". Standard library
only; one thread per request (a prediction runs its ensemble in parallel itself).
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .contract import NetwinsTwin, twin as default_twin

logger = logging.getLogger("netwins.service")
MAX_BODY = 8 * 1024 * 1024


def make_handler(t: NetwinsTwin, token: str = ""):
    class Handler(BaseHTTPRequestHandler):
        server_version = "netwins"

        def log_message(self, fmt, *args):
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: Any) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            if not token:
                return True
            given = self.headers.get("Authorization", "")
            return hmac.compare_digest(given, f"Bearer {token}")

        def _body(self) -> Optional[dict]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._send(413, {"error": f"a request is at most {MAX_BODY} bytes"})
                return None
            try:
                got = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self._send(400, {"error": "the body is not JSON"})
                return None
            if not isinstance(got, dict):
                self._send(400, {"error": "the body is a JSON object"})
                return None
            return got

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True, "service": "netwins"})
            if not self._allowed():
                return self._send(401, {"error": "this needs netwins' token"})
            if self.path == "/twin":
                return self._send(200, t.describe())
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._allowed():
                return self._send(401, {"error": "this needs netwins' token"})
            body = self._body()
            if body is None:
                return
            try:
                if self.path == "/twin/validate":
                    return self._send(200, t.validate(body.get("scenario")))
                if self.path == "/twin/predict":
                    return self._send(200, t.predict(body.get("scenario")))
                if self.path == "/twin/extra":
                    return self._send(200, t.extra(str(body.get("name") or ""), body.get("arguments") or {}))
                if self.path == "/twin/check_write":
                    return self._send(200, t.check_write(str(body.get("controller") or ""), str(body.get("tool") or ""),
                                                         body.get("arguments") or {}, body.get("answers") or {}))
            except Exception as e:  # noqa: BLE001 - said, with its kind, never a stack trace
                logger.exception("request failed")
                return self._send(500, {"error": f"{type(e).__name__}: {str(e)[:400]}"})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8420, t: Optional[NetwinsTwin] = None) -> ThreadingHTTPServer:
    token = os.getenv("NETWINS_TOKEN", "")
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        logger.warning("serving on %s with no NETWINS_TOKEN: anyone who can reach it can run the twins", host)
    srv = ThreadingHTTPServer((host, port), make_handler(t or default_twin(), token))
    return srv
