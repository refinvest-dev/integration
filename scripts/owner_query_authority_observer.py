"""Loopback TLS observation/fault hop; never supplies an authority verdict."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
import threading
from urllib.parse import urlsplit

PATH = "/internal/trading-review/v1/delegations/check"
BODY_LIMIT = 8192


def checked_target(origin):
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or parsed.hostname != "127.0.0.1" or not parsed.port or
            parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Observer requires a loopback HTTPS origin")
    return origin + PATH


class AuthorityObserver:
    def __init__(self, origin, ca, certificate, private_key):
        import requests

        self.target = checked_target(origin)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = str(ca)
        self.records = []
        self.lock = threading.Lock()
        self.hold_next = False
        self.held = threading.Event()
        self.release = threading.Event()
        self.released_forward = threading.Event()
        observer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.connection.settimeout(10)
                length = self.headers.get("Content-Length", "")
                if self.path != PATH or not length.isdecimal() or not 0 < int(length) <= BODY_LIMIT:
                    self.send_error(400)
                    return
                body = self.rfile.read(int(length))
                if len(body) != int(length):
                    return
                with observer.lock:
                    if len(observer.records) >= 256:
                        self.send_error(503)
                        return
                    observer.records.append({"parent": self.headers.get("traceparent", ""),
                                             "baggagePresent": "baggage" in self.headers})
                    held = observer.hold_next
                    observer.hold_next = False
                if held:
                    observer.held.set()
                    if not observer.release.wait(timeout=8):
                        self.send_error(503)
                        return
                    observer.released_forward.set()
                headers = {name: self.headers[name] for name in
                           ("Authorization", "Content-Type", "traceparent", "tracestate", "baggage") if name in self.headers}
                try:
                    with observer.session.post(observer.target, data=body, headers=headers,
                                               timeout=(2, 6), allow_redirects=False, stream=True) as response:
                        payload = response.raw.read(4097, decode_content=True)
                        if len(payload) > 4096:
                            self.send_error(502)
                            return
                        self.send_response(response.status_code)
                        for name in ("Content-Type", "Cache-Control"):
                            if name in response.headers:
                                self.send_header(name, response.headers[name])
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                except (requests.RequestException, OSError):
                    try:
                        self.send_error(502)
                    except OSError:
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate), str(private_key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.origin = "https://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, name="authority-observer")
        self.thread.start()

    def arm(self):
        with self.lock:
            self.held.clear()
            self.release.clear()
            self.released_forward.clear()
            self.hold_next = True

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.session.close()
        if self.thread.is_alive():
            raise RuntimeError("Observer did not stop")
