"""Local OAuth redirect server, shared by the auth entry point and the client.

Ownership note: `auth_indeed.py` is meant to be run as a script (module
`__main__`), whereas `app.indeed_client` imports this module by its package
path. Putting the server + wait logic in this dedicated module - and never in
the script module - guarantees both sides reference the exact same instance,
no matter how the script is launched.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


@dataclass
class CallbackResult:
    """Outcome of a successful (or not) OAuth redirect callback."""

    code: str
    state: str | None = None


class CallbackServer:
    """Captures the OAuth redirect (`code`, `state`) on 127.0.0.1."""

    def __init__(self, port: int):
        self.port = port
        self.code: str | None = None
        self.state: str | None = None
        self._event = threading.Event()
        self.httpd: ThreadingHTTPServer | None = None

    def start(self) -> None:
        """Bind + serve ready BEFORE any caller might try to open a browser."""
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self._make_handler())
        if not self.httpd.server_address:
            raise RuntimeError(f"Callback server failed to bind on 127.0.0.1:{self.port}")
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()

    def _make_handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                query = parse_qs(urlparse(self.path).query)
                owner.code = (query.get("code") or [None])[0]
                owner.state = (query.get("state") or [None])[0]
                body = b"<html><body><h3>Job-Hunt Agent: login complete.</h3>"
                body += b"<p>You can close this tab and return to the terminal.</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                owner._event.set()

            def log_message(self, *args):  # silence
                pass

        return Handler


_server: CallbackServer | None = None


def start_callback_server(port: int) -> CallbackServer:
    """Start (idempotently) and return the shared callback server instance."""
    global _server
    if _server is None:
        _server = CallbackServer(port)
        _server.start()
    return _server


def stop_callback_server() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None


async def wait_for_code(timeout: float = 120.0) -> CallbackResult:
    """Block (async) until the shared callback server receives the OAuth code."""
    server = _server
    if server is None:
        raise RuntimeError("Callback server not started. Call start_callback_server() first.")
    ok = await asyncio.to_thread(server._event.wait, timeout)
    if not ok:
        raise TimeoutError("Timed out waiting for the Indeed sign-in callback.")
    if not server.code:
        raise RuntimeError("Authorization failed: no code received in callback.")
    return CallbackResult(code=server.code, state=server.state)