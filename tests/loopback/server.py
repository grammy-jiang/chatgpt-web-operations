"""A fake chatgpt.com on 127.0.0.1 for the loopback tier (TESTING.md
section 6, P6).

The skill's real HTTP path runs against it unchanged: ``_common.open_session``
builds a real ``chatgpt_client.ChatGPTSession``, which builds a real
``chatgpt_session.Session``, which reads a synthetic Chrome cookie jar and
talks urllib to this server. Only ``chatgpt_session.BASE`` (or
``CHATGPT_BASE_URL`` in a subprocess) and the jar's location differ from
production.

What it answers:

- ``GET /api/auth/session``: a logged-in handshake with a bearer token and
  a ``Set-Cookie`` that renews the session token for 90 days (``Max-Age``
  7776000), the way chatgpt.com does;
- every registered read (``api_shapes.endpoint_for``): the recorded shape
  of that endpoint, synthesized (``api_shapes.synthesize``), so no account
  data is ever served or stored;
- anything else: 404, and the request is kept in ``unrecorded``.

A fault script, per path, is consumed one request at a time before any of
that: a status with a body (a Cloudflare 403 challenge, a 429, a 424), a
stall longer than the client's timeout, a body cut short, or an HTML page
with status 200. Every request is kept in ``requests`` (method, path,
headers) for the tests to read; nothing here prints a header.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import api_shapes  # noqa: E402

AUTH = "/api/auth/session"
ACCESS_TOKEN = "loopback-access-token"
USER_ID = "user-loopback"
RENEW_MAX_AGE = 7_776_000  # 90 days, what chatgpt.com sends
RENEWED_TOKEN = "loopback-renewed-session"
CHALLENGE_HEADERS = {"cf-mitigated": "challenge", "Content-Type": "text/html"}
CHALLENGE_BODY = "<!DOCTYPE html><title>Just a moment...</title>"


@dataclass(frozen=True)
class Fault:
    """One scripted answer. ``kind`` is ``reply`` (``status``, ``body``,
    ``headers``), ``stall`` (hold the connection ``seconds`` and answer
    nothing), ``cut`` (promise a longer body than is sent, then close) or
    ``html`` (a 200 whose body is a page, not JSON)."""

    kind: str
    status: int = 200
    body: Any = None
    headers: tuple[tuple[str, str], ...] = ()
    seconds: float = 0.0


def reply(
    status: int, body: Any = None, headers: dict[str, str] | None = None
) -> Fault:
    return Fault("reply", status, body, tuple((headers or {}).items()))


def challenge() -> Fault:
    """Cloudflare's 403: the shape of the two answers of 2026-09-23."""
    return reply(403, CHALLENGE_BODY, CHALLENGE_HEADERS)


def stall(seconds: float) -> Fault:
    return Fault("stall", seconds=seconds)


def cut() -> Fault:
    return Fault("cut")


def html() -> Fault:
    return Fault("html")


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes = b""


@dataclass
class LoopbackServer:
    shapes: dict[str, Any]
    prefer: dict[str, Any] = field(default_factory=dict)
    faults: dict[str, list[Fault]] = field(default_factory=dict)
    requests: list[Seen] = field(default_factory=list)
    unrecorded: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._release = threading.Event()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self.httpd.daemon_threads = True
        # A short poll, so stop() returns at once instead of after 0.5 s.
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> LoopbackServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._release.set()  # end every stall at once
        self.httpd.shutdown()
        self.httpd.server_close()

    def script(self, path: str, *faults: Fault) -> None:
        """Answer the next requests to ``path`` (query ignored) with ``faults``,
        in order; after them, the normal answer."""
        with self._lock:
            self.faults.setdefault(path, []).extend(faults)

    def seen(self, path: str) -> list[Seen]:
        return [r for r in self.requests if r.path.split("?", 1)[0] == path]

    # -- the answers -------------------------------------------------------
    def next_fault(self, path: str) -> Fault | None:
        with self._lock:
            queue = self.faults.get(path) or []
            return queue.pop(0) if queue else None

    def answer(self, method: str, raw_path: str) -> tuple[int, Any, dict[str, str]]:
        path = raw_path.split("?", 1)[0]
        if path == AUTH:
            cookie = (
                f"__Secure-next-auth.session-token={RENEWED_TOKEN}; Path=/; "
                f"Max-Age={RENEW_MAX_AGE}; HttpOnly; Secure; SameSite=Lax"
            )
            body = {
                "user": {"id": USER_ID, "name": "Loopback"},
                "expires": "2099-01-01T00:00:00.000Z",
                "accessToken": ACCESS_TOKEN,
            }
            return 200, body, {"Set-Cookie": cookie}
        endpoint = api_shapes.endpoint_for(method, raw_path)
        shape = self.shapes.get(endpoint.name) if endpoint else None
        if shape is None:
            self.unrecorded.append(f"{method} {raw_path}")
            return 404, {"detail": "not a recorded read"}, {}
        return 200, api_shapes.synthesize(shape, None, endpoint.name, self.prefer), {}


def _handler_for(server: LoopbackServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # quiet: headers carry tokens
            return

        def _send(self, status: int, body: Any, headers: dict[str, str]) -> None:
            if isinstance(body, bytes):
                data = body
            elif isinstance(body, str):
                data = body.encode()
            else:
                data = json.dumps(body).encode()
            self.send_response(status)
            names = {k.lower() for k in headers}
            if "content-type" not in names:
                self.send_header("Content-Type", "application/json")
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            with server._lock:
                headers = {k.lower(): v for k, v in self.headers.items()}
                server.requests.append(Seen(method, self.path, headers, body))
            fault = server.next_fault(self.path.split("?", 1)[0])
            try:
                if fault is None:
                    self._send(*server.answer(method, self.path))
                elif fault.kind == "reply":
                    self._send(fault.status, fault.body or "", dict(fault.headers))
                elif fault.kind == "stall":
                    server._release.wait(fault.seconds)
                    self.close_connection = True
                elif fault.kind == "cut":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "4096")
                    self.end_headers()
                    self.wfile.write(b'{"items": [')
                    self.close_connection = True
                elif fault.kind == "html":
                    self._send(200, CHALLENGE_BODY, {"Content-Type": "text/html"})
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # the client gave up first

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PATCH(self) -> None:
            self._handle("PATCH")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

    return Handler


def write_jar(path: Path, *, session_token: str = "loopback-session") -> Path:
    """A Chrome cookie database with what ``_cookie_pairs`` reads: a plain
    session token, a v10-encrypted ``cf_clearance`` (Chrome's fixed
    "peanuts" key, so no keyring is needed), an empty analytics cookie, and
    a cookie of another site that must not be sent."""
    import sqlite3

    from cryptography.hazmat.primitives import hashes, padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    key = PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1
    ).derive(b"peanuts")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(b"loopback-clearance") + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    v10 = b"v10" + encryptor.update(padded) + encryptor.finalize()
    far = (4_102_444_800 + 11_644_473_600) * 1_000_000  # 2100, webkit microseconds
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, expires_utc INTEGER)"
    )
    con.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?)",
        [
            (
                ".chatgpt.com",
                "__Secure-next-auth.session-token",
                session_token,
                b"",
                far,
            ),
            (".chatgpt.com", "cf_clearance", "", v10, far),
            ("chatgpt.com", "_dd_s", "", b"", 0),
            (".example.com", "elsewhere", "must-not-be-sent", b"", 0),
        ],
    )
    con.commit()
    con.close()
    return path
