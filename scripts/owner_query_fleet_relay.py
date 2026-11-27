"""Owned loopback TLS hops for a synthetic fleet; no identity or authority decisions."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import ssl
import threading
from urllib.parse import urlsplit

AUTHORITY_PATH = "/internal/trading-review/v1/delegations/check"
METADATA_PATH = re.compile(r"/internal/trading-review/v1/(accounts|books)/[1-9][0-9]{0,18}")


def loopback_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("A fixed loopback TLS origin is required")
    return value


class ServingTargets:
    """Fixture routing membership; a live health endpoint alone never confirms a node."""
    def __init__(self):
        self._targets = {}
        self._serving = set()
        self._selected = None
        self._diagnostic = False
        self._lock = threading.Lock()

    def register(self, label, origin):
        origin = loopback_origin(origin)
        if not re.fullmatch(r"(core|go)-[ab]", label):
            raise ValueError("Unknown fixture node")
        with self._lock:
            if label in self._targets:
                raise ValueError("Node already registered")
            self._targets[label] = origin

    def confirm(self, label):
        with self._lock:
            if label not in self._targets:
                raise ValueError("Unknown fixture node")
            self._serving.add(label)

    def exclude(self, label):
        with self._lock:
            self._serving.discard(label)
            if self._selected == label:
                self._selected = next(iter(sorted(self._serving)), None)
                self._diagnostic = False

    def select(self, label, *, diagnostic=False):
        with self._lock:
            if label not in self._targets or (label not in self._serving and not diagnostic):
                raise ValueError("Unconfirmed fixture node")
            self._selected, self._diagnostic = label, diagnostic

    def target(self):
        with self._lock:
            if self._selected is None:
                raise ValueError("No serving fixture node")
            return self._selected, self._targets[self._selected], self._diagnostic


class TokenSlot:
    """At most one delegation in private memory, never part of evidence or repr."""
    def __init__(self):
        self._value = None
        self._lock = threading.Lock()

    def put(self, value):
        with self._lock:
            self._value = value if value and len(value) <= 4096 else None

    def peek(self):
        with self._lock:
            return self._value

    def clear(self):
        self.put(None)

    def __repr__(self):
        return "TokenSlot(redacted)"


class FleetRelay:
    def __init__(self, role, ca, certificate, private_key, *, body_fault=None):
        if body_fault is not None and role != "metadata":
            raise ValueError("Body faults are restricted to metadata")
        import requests

        if role not in ("metadata", "authority"):
            raise ValueError("Unknown relay role")
        self.role = role
        self._body_fault = body_fault
        self.targets = ServingTargets()
        self.token = TokenSlot()
        self.counts = {"forwarded": 0, "diagnostic": 0}
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.verify = str(ca)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate), str(private_key))
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(10)
                self.request = context.wrap_socket(self.request, server_side=True)
                super().setup()

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                metadata = role == "metadata" and self.command == "GET" and METADATA_PATH.fullmatch(self.path)
                authority = role == "authority" and self.command == "POST" and self.path == AUTHORITY_PATH
                length = self.headers.get("Content-Length", "0")
                names = ("Authorization", "X-RefInvest-User-Delegation", "Content-Type", "Accept", "Cache-Control",
                         "traceparent", "tracestate", "baggage")
                if (not (metadata or authority) or not length.isdecimal() or int(length) > 8192 or
                        (metadata and int(length) != 0) or "Transfer-Encoding" in self.headers or
                        any(len(self.headers.get_all(name, [])) > 1 for name in names)):
                    self.send_error(400)
                    return
                body = self.rfile.read(int(length))
                if len(body) != int(length):
                    return
                try:
                    label, origin, diagnostic = relay.targets.target()
                    headers = {name: self.headers[name] for name in names if name in self.headers}
                    headers["Connection"] = "close"
                    with relay._session.request(self.command, origin + self.path, data=body or None, headers=headers,
                                                timeout=(2, 8), allow_redirects=False, stream=True) as response:
                        payload = response.raw.read(8193, decode_content=True)
                        if len(payload) > 8192:
                            self.send_error(502)
                            return
                        with relay._lock:
                            relay.counts["forwarded"] += 1
                            relay.counts["diagnostic"] += int(diagnostic)
                        if metadata and response.status_code == 200:
                            relay.token.put(headers.get("X-RefInvest-User-Delegation"))
                        fault = relay._body_fault.take() if metadata and response.status_code == 200 and relay._body_fault else None
                        if fault:
                            self.protocol_version = fault.protocol
                        self.send_response(response.status_code)
                        for name in ("Content-Type", "Cache-Control", "X-Content-Type-Options"):
                            if name in response.headers:
                                self.send_header(name, response.headers[name])
                        framing = fault.headers(payload) if fault else {"Content-Length": str(len(payload))}
                        for name, value in framing.items():
                            self.send_header(name, value)
                        self.end_headers()
                        if fault:
                            fault.write(self, payload)
                        else:
                            self.wfile.write(payload)
                except (requests.RequestException, OSError, ValueError):
                    try:
                        self.send_error(502)
                    except OSError:
                        pass

        class Server(ThreadingHTTPServer):
            daemon_threads = False

            def handle_error(self, *_):
                pass

        self._server = Server(("127.0.0.1", 0), Handler)
        self.origin = "https://127.0.0.1:" + str(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, name="fleet-" + role)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)
        self._session.close()
        self.token.clear()
        if self._thread.is_alive():
            raise RuntimeError("Fixture relay did not stop")
