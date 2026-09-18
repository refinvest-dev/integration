"""Integration-only proxy that drops the first successful Compute submission response."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class FaultState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.forwarded_posts = 0
        self.dropped_accepted_responses = 0
        self.retry_requests = 0
        self.upstream_failures = 0
        self.upstream_status_counts: dict[int, int] = {}
        self.last_upstream_error: str | None = None
        self.first_key_hash: str | None = None
        self.first_request_body_hash: str | None = None
        self.dropped_compute_run_id: str | None = None

    def observe_post(self, key: str | None, body: bytes) -> tuple[str | None, str]:
        key_hash = hashlib.sha256(key.encode()).hexdigest() if key else None
        body_hash = hashlib.sha256(body).hexdigest()
        with self.lock:
            self.forwarded_posts += 1
            if (
                self.first_key_hash is not None
                and key_hash == self.first_key_hash
                and body_hash == self.first_request_body_hash
            ):
                self.retry_requests += 1
        return key_hash, body_hash

    def should_drop(
        self,
        status: int,
        key_hash: str | None,
        request_body_hash: str | None,
        response_body: bytes,
    ) -> bool:
        with self.lock:
            self.upstream_status_counts[status] = self.upstream_status_counts.get(status, 0) + 1
            if status != 202 or self.dropped_accepted_responses != 0:
                return False
            self.dropped_accepted_responses = 1
            self.first_key_hash = key_hash
            self.first_request_body_hash = request_body_hash
            try:
                self.dropped_compute_run_id = str(json.loads(response_body)["runId"])
            except (KeyError, TypeError, ValueError):
                self.dropped_compute_run_id = None
            return True

    def observe_upstream_failure(self, error: OSError) -> None:
        with self.lock:
            self.upstream_failures += 1
            self.last_upstream_error = type(error).__name__

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "forwardedSubmissionRequests": self.forwarded_posts,
                "droppedAcceptedResponses": self.dropped_accepted_responses,
                "retryRequests": self.retry_requests,
                "sameKeyRetryObserved": self.retry_requests > 0,
                "droppedComputeRunId": self.dropped_compute_run_id,
                "upstreamFailures": self.upstream_failures,
                "upstreamStatusCounts": self.upstream_status_counts,
                "lastUpstreamError": self.last_upstream_error,
            }


class FaultProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RefInvestIntegrationFaultProxy/1"

    def do_GET(self) -> None:
        if self.path == "/__faults__/status":
            self._send_json(200, self.server.fault_state.snapshot())
            return
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        request_body = self._read_request_body()
        key_hash = None
        request_body_hash = None
        if self.command == "POST" and self.path == "/backtests":
            key_hash, request_body_hash = self.server.fault_state.observe_post(
                self.headers.get("Idempotency-Key"), request_body
            )

        upstream = self.server.upstream
        connection = http.client.HTTPConnection(
            upstream.hostname, upstream.port, timeout=self.server.upstream_timeout
        )
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower()
            not in {
                "connection",
                "content-length",
                "host",
                "proxy-connection",
                "transfer-encoding",
            }
        }
        try:
            connection.request(self.command, self.path, body=request_body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            response_headers = response.getheaders()
        except OSError as error:
            self.server.fault_state.observe_upstream_failure(error)
            self._send_json(502, {"message": "Compute upstream unavailable."})
            return
        finally:
            connection.close()

        if (
            self.command == "POST"
            and self.path == "/backtests"
            and self.server.fault_state.should_drop(
                response.status, key_hash, request_body_hash, response_body
            )
        ):
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return

        self.send_response(response.status)
        for name, value in response_headers:
            if name.lower() not in {
                "connection",
                "content-length",
                "date",
                "server",
                "transfer-encoding",
            }:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def _read_request_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        if transfer_encoding == "chunked":
            chunks: list[bytes] = []
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    raise ConnectionError("Missing chunk size.")
                size = int(size_line.split(b";", 1)[0], 16)
                if size == 0:
                    while self.rfile.readline() not in {b"\r\n", b"\n", b""}:
                        pass
                    break
                chunks.append(self.rfile.read(size))
                if self.rfile.read(2) != b"\r\n":
                    raise ConnectionError("Invalid chunk terminator.")
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class FaultProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], upstream: str, upstream_timeout: float
    ) -> None:
        parsed = urlsplit(upstream)
        if parsed.scheme != "http" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("Upstream must be an HTTP origin without a path.")
        self.upstream = parsed
        self.upstream_timeout = upstream_timeout
        self.fault_state = FaultState()
        super().__init__(address, FaultProxyHandler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--upstream", default="http://127.0.0.1:8000")
    parser.add_argument("--upstream-timeout", type=float, default=30.0)
    args = parser.parse_args()
    FaultProxyServer((args.bind, args.port), args.upstream, args.upstream_timeout).serve_forever()


if __name__ == "__main__":
    main()
