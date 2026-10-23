"""The local HTTP server: stdlib only, bound to 127.0.0.1, protected by a per-launch token.

Defences, in order of the request: Host header must be a loopback name with our port (DNS-rebinding), the token must arrive as a
header or the session cookie, state-changing requests must also carry ``X-Documaster: 1`` and, when the browser sends an Origin,
it must be ours (CSRF), bodies are size-limited, and every response sets a strict CSP, ``nosniff`` and ``no-referrer``.
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

from . import api as _api  # noqa: F401  (registers routes)
from . import api_analysis as _api_analysis  # noqa: F401
from .app import App
from .errors import DocumasterError
from .files import ensure_inside
from .router import Request, Response, ok, router

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
COOKIE = "dm_session"
MAX_HEADER_QUERY = 8192


class DocumasterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app: App, client_dir: Path | None) -> None:
        self.app = app
        self.client_dir = client_dir
        super().__init__((app.settings.host, app.settings.port), Handler)

    @property
    def base_url(self) -> str:
        return f"http://{self.server_address[0]}:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    server: DocumasterServer
    protocol_version = "HTTP/1.1"
    server_version = "Documaster"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:       # quiet; errors are logged explicitly
        if os.environ.get("DOCUMASTER_LOG_REQUESTS"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------------ plumbing
    def do_GET(self) -> None: self._handle("GET")
    def do_POST(self) -> None: self._handle("POST")
    def do_PUT(self) -> None: self._handle("PUT")
    def do_DELETE(self) -> None: self._handle("DELETE")
    def do_HEAD(self) -> None: self._handle("GET", head=True)

    def do_OPTIONS(self) -> None:
        self._send(Response(405, json.dumps({"ok": False, "error": {"code": "method_not_allowed",
                                                                    "message": "Cross-origin requests are not accepted"}}).encode()))

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

    def _token_ok(self, query: dict[str, str]) -> bool:
        token = self.server.app.settings.token
        supplied = self.headers.get("X-Documaster-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            supplied = auth[7:]
        if not supplied:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    supplied = v
        return bool(supplied) and hmac.compare_digest(supplied, token)

    def _handle(self, method: str, head: bool = False) -> None:
        try:
            self._dispatch(method, head)
        except DocumasterError as exc:
            self._send_error(exc.status, exc.to_dict(), head)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            self._send_error(500, {"code": "internal_error", "message": f"Unexpected error ({type(exc).__name__})", "details": {}}, head)

    def _dispatch(self, method: str, head: bool) -> None:
        split = urlsplit(self.path)
        if len(self.path) > MAX_HEADER_QUERY:
            return self._send_error(414, {"code": "uri_too_long", "message": "The address is too long"}, head)
        if not self._host_ok():
            return self._send_error(421, {"code": "bad_host", "message": "This server only answers on its own address"}, head)
        path = split.path
        query = dict(parse_qsl(split.query, keep_blank_values=True))
        if not path.startswith("/api/"):
            return self._static(path, query, method, head)
        if not self._token_ok(query):
            return self._send_error(401, {"code": "unauthorized", "message": "Open Documaster from the link printed when it started"}, head)
        origin = self.headers.get("Origin")
        found = router.match(method, path)
        if found is None:
            return self._send_error(404, {"code": "not_found", "message": "No such endpoint"}, head)
        if found == "method":
            return self._send_error(405, {"code": "method_not_allowed", "message": "That method is not allowed here"}, head)
        handler, params, mutating = found  # type: ignore[misc]
        if mutating:
            if self.headers.get("X-Documaster") != "1":
                return self._send_error(403, {"code": "csrf", "message": "Missing request marker"}, head)
            if origin and origin not in (f"http://127.0.0.1:{self.server.server_address[1]}", f"http://localhost:{self.server.server_address[1]}"):
                return self._send_error(403, {"code": "bad_origin", "message": "Requests from other sites are not accepted"}, head)
        length = self.headers.get("Content-Length")
        n = int(length) if length and length.isdigit() else None
        req = Request(method, path, query, {k.lower(): v for k, v in self.headers.items()}, self.rfile, n, params)
        result = handler(self.server.app, req)
        resp = result if isinstance(result, Response) else ok(result)
        # drain an unread body so the connection stays usable
        if n and not req._read and method in ("POST", "PUT") and handler.__name__ not in ("upload", "upload_asset"):
            self.rfile.read(min(n, 1 << 20))
        self._send(resp, head)

    # ------------------------------------------------------------------ static client
    def _static(self, path: str, query: dict[str, str], method: str, head: bool) -> None:
        if method != "GET":
            return self._send_error(405, {"code": "method_not_allowed", "message": "Not allowed"}, head)
        if path == "/" and query.get("token"):
            if hmac.compare_digest(query["token"], self.server.app.settings.token):
                resp = Response(302, b"", "text/plain", {"Location": "/",
                                                          "Set-Cookie": f"{COOKIE}={query['token']}; Path=/; HttpOnly; SameSite=Strict"})
                return self._send(resp, head)
            return self._send_error(401, {"code": "unauthorized", "message": "That link is not valid for this session"}, head)
        root = self.server.client_dir
        if root is None or not root.exists():
            return self._send(Response(503, b"The Documaster client has not been built (run `npm run build` in applications/Documaster).",
                                       "text/plain; charset=utf-8"), head)
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        try:
            target = ensure_inside(root, root / rel)
        except Exception:  # noqa: BLE001
            return self._send_error(404, {"code": "not_found", "message": "Not found"}, head)
        if not target.is_file():
            return self._send_error(404, {"code": "not_found", "message": "Not found"}, head)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix in (".js", ".mjs"):
            ctype = "text/javascript"
        self._send(Response(200, b"", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("json") else ""),
                            {"Cache-Control": "no-cache"}, file_path=str(target)), head)

    # ------------------------------------------------------------------ responses
    def _send_error(self, status: int, error: dict[str, Any], head: bool = False) -> None:
        body = json.dumps({"ok": False, "error": error}, ensure_ascii=False, default=str).encode("utf-8")
        self._send(Response(status, body), head)

    def _send(self, resp: Response, head: bool = False) -> None:
        self.send_response(resp.status, HTTPStatus(resp.status).phrase if resp.status in HTTPStatus._value2member_map_ else "")
        self.send_header("Content-Type", resp.content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if "Cache-Control" not in resp.headers:
            self.send_header("Cache-Control", "no-store")
        for k, v in resp.headers.items():
            self.send_header(k, v)
        if resp.download_name:
            ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in resp.download_name)
            self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(resp.download_name)}")
        if resp.file_path:
            size = os.path.getsize(resp.file_path)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if not head:
                with open(resp.file_path, "rb") as fh:
                    while chunk := fh.read(1 << 20):
                        self.wfile.write(chunk)
            return
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if not head:
            self.wfile.write(resp.body)
