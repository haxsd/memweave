import hmac
import json
import secrets
import socket
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from memweave.diagnostics import DiagnosticError


MAX_BODY_BYTES = 256 * 1024


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, service, port, preferred_root=None):
        self.service = service
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), LocalRequestHandler)
        self.timeout = 0.5
        parameters = {"token": self.token}
        if preferred_root is not None:
            root = Path(preferred_root).expanduser()
            if root.is_dir():
                parameters["project"] = str(root.resolve())
        self.url = f"http://127.0.0.1:{self.server_port}/?{urlencode(parameters)}"

    def handle_error(self, request, client_address):
        pass


class LocalRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 5

    def log_message(self, format, *arguments):
        pass

    def _respond(self, status, payload, content_type="application/json; charset=utf-8"):
        if isinstance(payload, dict):
            payload = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; font-src 'self'; object-src 'none'; "
            "frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _error(self, status, code, message):
        self.close_connection = True
        self._respond(status, {"status": "error", "error": {"code": code, "message": message}})
        self.wfile.flush()
        try:
            self.connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + 0.1
            remaining = MAX_BODY_BYTES
            while remaining > 0 and time.monotonic() < deadline:
                self.connection.settimeout(max(0.001, deadline - time.monotonic()))
                discarded = self.rfile.read1(min(remaining, 65536))
                if not discarded:
                    break
                remaining -= len(discarded)
        except OSError:
            pass

    def send_error(self, code, message=None, explain=None):
        self._error(code, "HTTP_ERROR", "The HTTP request is not supported or is malformed.")

    def _trusted_request(self):
        hosts = self.headers.get_all("Host", [])
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if len(hosts) != 1 or hosts[0] not in allowed:
            self._error(403, "SCOPE_DENIED", "Use the local server address.")
            return False
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0] != f"http://{hosts[0]}"):
            self._error(403, "SCOPE_DENIED", "Cross-origin requests are not allowed.")
            return False
        fetch_sites = self.headers.get_all("Sec-Fetch-Site", [])
        if fetch_sites and (len(fetch_sites) != 1 or fetch_sites[0] not in ("same-origin", "none")):
            self._error(403, "SCOPE_DENIED", "Cross-site requests are not allowed.")
            return False
        return True

    def _authenticated(self):
        authorizations = self.headers.get_all("Authorization", [])
        expected = f"Bearer {self.server.token}".encode("utf-8")
        supplied = authorizations[0].encode("utf-8") if len(authorizations) == 1 else b""
        if not hmac.compare_digest(supplied, expected):
            self._error(401, "AUTH_REQUIRED", "Supply the local session bearer token.")
            return False
        return True

    def _path(self):
        if not self.path.startswith("/"):
            self._error(400, "RECORD_INVALID", "Use an absolute local path.")
            return None
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self._error(400, "RECORD_INVALID", "Use a valid local path.")
            return None
        if parsed.scheme or parsed.netloc or parsed.fragment:
            self._error(400, "RECORD_INVALID", "Use an absolute local path.")
            return None
        return parsed.path

    def do_GET(self):
        if not self._trusted_request():
            return
        path = self._path()
        if path is None:
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
            self._error(400, "RECORD_INVALID", "GET requests cannot contain a body.")
            return
        if path == "/":
            self._respond(
                200, files("memweave").joinpath("static", "index.html").read_bytes(),
                "text/html; charset=utf-8",
            )
        elif path.startswith("/api/"):
            if not self._authenticated():
                return
            if path != "/api/status":
                self._error(405, "METHOD_NOT_ALLOWED", "Use POST for service actions.")
                return
            self._invoke("status", {})
        else:
            self._error(404, "NOT_FOUND", "Resource not found.")

    def do_POST(self):
        if not self._trusted_request() or not self._authenticated():
            return
        path = self._path()
        if path is None:
            return
        if not path.startswith("/api/") or "/" in path[len("/api/"):]:
            self._error(404, "NOT_FOUND", "Resource not found.")
            return
        parameters = self._parameters()
        if parameters is not None:
            self._invoke(path[len("/api/"):], parameters)

    def handle_expect_100(self):
        self._error(417, "RECORD_INVALID", "Expect requests are not supported.")
        return False

    def _parameters(self):
        if self.headers.get_all("Transfer-Encoding"):
            self._error(400, "RECORD_INVALID", "Transfer encodings are not supported.")
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self._error(400, "RECORD_INVALID", "Supply one nonnegative Content-Length.")
            return None
        normalized_length = lengths[0].lstrip("0") or "0"
        if len(normalized_length) > 6 or int(normalized_length) > MAX_BODY_BYTES:
            self._error(413, "RECORD_INVALID", "JSON body exceeds 256 KiB.")
            return None
        length = int(normalized_length)
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or content_types[0].split(";")[0].strip().lower() != "application/json":
            self._error(415, "RECORD_INVALID", "Use application/json.")
            return None
        if self.headers.get_all("Content-Encoding"):
            self._error(415, "RECORD_INVALID", "Content encodings are not supported.")
            return None
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request body")
            parameters = json.loads(
                raw.decode("utf-8"), parse_constant=self._invalid_constant,
            )
            if not isinstance(parameters, dict):
                raise ValueError("Parameters must be an object")
        except (ValueError, UnicodeError, RecursionError):
            self._error(400, "RECORD_INVALID", "Supply a valid UTF-8 JSON parameter object.")
            return None
        except (TimeoutError, OSError):
            self._error(408, "RECORD_INVALID", "Request body was not received in time.")
            return None
        return parameters

    @staticmethod
    def _invalid_constant(value):
        raise ValueError("Non-finite JSON numbers are not supported")

    def _invoke(self, action, parameters):
        try:
            output = self.server.service.dispatch(action, parameters)
        except DiagnosticError as error:
            status = 409 if error.code == "VERSION_CONFLICT" else 400
            self._error(status, error.code, str(error))
        except Exception:
            self._error(500, "INTERNAL_ERROR", "The local service could not complete the action.")
        else:
            self._respond(200, output)


def create_http_server(service, port=0, preferred_root=None):
    return LocalHTTPServer(service, port, preferred_root)


def serve_ui(service, port=8765, open_browser=True, preferred_root=None):
    with create_http_server(service, port, preferred_root) as server:
        print(server.url, flush=True)
        if open_browser:
            webbrowser.open(server.url)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
