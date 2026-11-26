# SPDX-License-Identifier: GPL-3.0-or-later
"""Local loopback control channel used by the Windows GUI and deckd."""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading

log = logging.getLogger("deckd")

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 4651
MAX_REQUEST_BYTES = 16_384


class _ControlRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(2.0)
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            self._reply(error="invalid or oversized request")
            return
        try:
            request = json.loads(raw)
            method = request["method"]
            args = request.get("args", [])
            if not isinstance(method, str) or not isinstance(args, list):
                raise ValueError("request must contain a method and argument list")
            result = self.server.control.dispatch(method, args)
            self._reply(result=result)
        except (KeyError, TypeError, ValueError) as error:
            self._reply(error=str(error))
        except Exception:
            log.exception("Windows control request failed")
            self._reply(error="control request failed")

    def _reply(self, **payload) -> None:
        try:
            self.wfile.write(json.dumps(payload).encode("utf-8") + b"\n")
        except OSError:
            log.debug("Windows control client disconnected while replying")


class _ThreadedControlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address, control):
        self.control = control
        super().__init__(address, _ControlRequestHandler)


class WindowsControlServer:
    """Serve a restricted JSON control API on the IPv4 loopback interface."""

    def __init__(self, control, host: str = CONTROL_HOST, port: int = CONTROL_PORT):
        self._server = _ThreadedControlServer((host, port), control)
        self.host, self.port = self._server.server_address
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="deck-control-windows",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        log.info("Windows control interface listening on %s:%d", self.host, self.port)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class WindowsControlClient:
    """Synchronous client for the loopback control API."""

    def __init__(
        self,
        host: str = CONTROL_HOST,
        port: int = CONTROL_PORT,
        timeout: float = 1.0,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout

    def call(self, method: str, *args):
        request = json.dumps({"method": method, "args": list(args)}).encode("utf-8") + b"\n"
        if len(request) > MAX_REQUEST_BYTES:
            raise ValueError("control request is too large")
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as connection:
            connection.settimeout(self.timeout)
            connection.sendall(request)
            with connection.makefile("rb") as response_file:
                raw = response_file.readline(MAX_REQUEST_BYTES + 1)
        if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            raise OSError("invalid response from Windows control service")
        response = json.loads(raw)
        if not isinstance(response, dict):
            raise OSError("invalid response from Windows control service")
        if "error" in response:
            raise RuntimeError(response["error"])
        if "result" not in response:
            raise OSError("missing result from Windows control service")
        return response["result"]
