"""One-shot metadata transport faults; no verdict, identity, or payload is recorded."""
from __future__ import annotations

import select
import ssl
import threading
import time

MAX_BODY = 16 * 1024
KINDS = frozenset(("partial", "truncate", "declared_oversize", "stream_overflow"))


def overflow_payload(payload, max_body=MAX_BODY):
    if max_body not in (MAX_BODY, 2 * 1024 * 1024):
        raise ValueError("Unknown fixture body limit")
    return payload + b" " * max(0, max_body + 1 - len(payload))


class BodyFault:
    def __init__(self, kind, max_body=MAX_BODY):
        if kind not in KINDS:
            raise ValueError("Unknown metadata fault")
        if max_body not in (MAX_BODY, 2 * 1024 * 1024):
            raise ValueError("Unknown fixture body limit")
        self.max_body = max_body
        self.kind = kind
        self.entered = threading.Event()
        self.release = threading.Event()
        self.peer_closed = threading.Event()
        self.finished = threading.Event()
        self.started = False
        self.prefix_sent = False
        self.guard_expired = False
        self.io_failure = False
        self.peer_close_source = None
        self.peer_closed_before_release = False
        self.protocol = "HTTP/1.1" if kind == "stream_overflow" else "HTTP/1.0"

    def headers(self, payload):
        if self.kind == "stream_overflow":
            return {"Transfer-Encoding": "chunked", "Connection": "close"}
        length = self.max_body + 1 if self.kind == "declared_oversize" else len(payload)
        return {"Content-Length": str(length), "Connection": "close"}

    def _closed(self, source):
        self.peer_close_source = source
        self.peer_closed_before_release = not self.release.is_set()
        self.peer_closed.set()

    def _send(self, handler, body):
        handler.wfile.write(body)
        handler.wfile.flush()

    def _await_release_or_peer(self, handler):
        deadline = time.monotonic() + 15
        connection = handler.connection
        previous = connection.gettimeout()
        try:
            connection.settimeout(0.05)
            while not self.release.is_set() and time.monotonic() < deadline:
                if select.select([connection], [], [], 0.025)[0]:
                    try:
                        data = connection.recv(1)
                        if data == b"":
                            self._closed("eof")
                            return False
                        self.io_failure = True
                        return False
                    except (ssl.SSLWantReadError, TimeoutError):
                        continue
            if not self.release.is_set():
                self.guard_expired = True
            return self.release.is_set()
        finally:
            connection.settimeout(previous)

    def write(self, handler, payload):
        self.started = True
        try:
            if self.kind == "stream_overflow":
                body = overflow_payload(payload, self.max_body)
                self._send(handler, format(len(body), "x").encode("ascii") + b"\r\n" + body + b"\r\n")
            else:
                prefix = payload[:max(1, len(payload) // 2)] if self.kind == "truncate" else payload[:1]
                self._send(handler, prefix)
            self.prefix_sent = True
            self.entered.set()
            if self.kind == "truncate":
                return
            if self._await_release_or_peer(handler):
                if self.kind == "partial":
                    self._send(handler, payload[1:])
                elif self.kind == "stream_overflow":
                    self._send(handler, b"0\r\n\r\n")
        except (ssl.SSLEOFError, ssl.SSLZeroReturnError):
            self._closed("tls-eof")
        except ConnectionResetError:
            self._closed("reset")
        except BrokenPipeError:
            self._closed("broken-pipe")
        except OSError:
            self.io_failure = True
        finally:
            self.entered.set()
            self.finished.set()

    def observation(self):
        return {"kind": self.kind, "started": self.started, "prefixSent": self.prefix_sent,
                "peerClosed": self.peer_closed.is_set(), "peerCloseSource": self.peer_close_source,
                "peerClosedBeforeRelease": self.peer_closed_before_release,
                "releaseRequested": self.release.is_set(), "guardExpired": self.guard_expired,
                "ioFailure": self.io_failure, "finished": self.finished.is_set()}


class BodyFaults:
    def __init__(self, max_body=MAX_BODY):
        if max_body not in (MAX_BODY, 2 * 1024 * 1024):
            raise ValueError("Unknown fixture body limit")
        self.max_body = max_body
        self._pending = None
        self.records = []
        self._lock = threading.Lock()

    def arm(self, kind):
        fault = BodyFault(kind, self.max_body)
        with self._lock:
            if self._pending is not None or len(self.records) >= 16:
                raise ValueError("Metadata fault already pending or fixture budget exhausted")
            self.records.append(fault)
            self._pending = fault
        return fault

    def take(self):
        with self._lock:
            fault, self._pending = self._pending, None
            return fault

    def close(self):
        with self._lock:
            for fault in self.records:
                fault.release.set()
