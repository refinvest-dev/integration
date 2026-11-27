"""Ordinary Main response-body faults for existing ADR-086 metadata reads."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import shutil
import socket
import ssl
import time
import traceback
from pathlib import Path

from owner_query_body_fault import BodyFaults
from owner_query_e2e import ACCOUNT, BOOK, TRACE_PARENT, private_write, require
from owner_query_fleet_relay import FleetRelay
from owner_query_main_e2e import MainServiceAcceptance


class TransportServiceAcceptance(MainServiceAcceptance):
    body_faults = None
    metadata_relay = None

    def execute(self):
        try:
            super().execute()
        except Exception as error:
            # Only source locations/types; exception messages may contain credentials or URLs.
            self.evidence["failureFrames"] = [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(error.__traceback__)]
            self.evidence["failureTypes"] = []
            seen = set()
            while error is not None and id(error) not in seen:
                seen.add(id(error))
                self.evidence["failureTypes"].append(type(error).__name__)
                error = error.__cause__ or error.__context__
            raise

    def configure(self):
        super().configure()
        self.body_faults = BodyFaults()
        self.metadata_relay = FleetRelay("metadata", self.directory / "root-ca.pem", self.directory / "observer-cert.pem",
                                         self.directory / "observer-key.pem", body_fault=self.body_faults)
        self.metadata_relay.targets.register("go-a", self.go_origin)
        self.metadata_relay.targets.select("go-a", diagnostic=True)
        path = self.directory / "core-runtime.json"
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot.update(goOrigin=self.metadata_relay.origin, requestTimeout="PT3S", connectTimeout="PT1S",
                        queryTimeoutSeconds=1, maxInFlight=1)
        private_write(path, snapshot)
        self.go_config.update(requestTimeout="2s", authorityTimeout="1s")
        private_write(self.directory / "go-runtime.json", self.go_config)
        self.evidence.update(coreMode="ordinary-main-transport", testInitializer=False, operationalActivation=False,
            topology="Core Main -> metadata fault TLS relay -> Go Main -> authority TLS observer -> Core Main",
            fixtureBudgets={"coreRequestSeconds": 3, "goRequestSeconds": 2, "authoritySeconds": 1, "coreMaxInFlight": 1})

    def record_sources(self):
        super().record_sources()
        self.evidence["transportHarnessSources"] = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ("owner_query_e2e.py", "owner_query_main_e2e.py", "owner_query_authority_observer.py",
                         "owner_query_fleet_relay.py", "owner_query_body_fault.py", "owner_query_transport_e2e.py")}

    def baseline(self):
        before = self.metadata_relay.counts["forwarded"]
        for path, identity in (("/trading-accounts/" + ACCOUNT, ACCOUNT), ("/trading-books/" + BOOK, BOOK)):
            require(self.request(path).json()["id"] == identity, "TRANSPORT_METADATA_BINDING")
        require(self.metadata_relay.counts["forwarded"] == before + 2 and len(self.observations()) >= 2, "TRANSPORT_REAL_HOPS_MISSING")
        self.metadata_relay.targets.confirm("go-a")
        self.metadata_relay.targets.select("go-a")
        self.mark("actual_core_go_authority_and_both_metadata_resources_before_faults")

    def scenarios(self):
        self.inspect_beans()
        self.baseline()
        self.checkpoint("BODY_FAULTS")
        self.deadline_and_slot()
        for kind in ("truncate", "declared_oversize", "stream_overflow"):
            self.rejected_body(kind)
        self.browser_abort()
        self.healthy_body_drain()
        self.settings["trading-review.owner-query-runtime.drain-timeout"] = "PT0.1S"
        self.update_core_environment()
        java = str(self.args.java or shutil.which("java") or "java")
        self.core_process = self.start("core-transport-short-drain", [java, "@" + str(self.directory / "core.args")],
                                       self.core_env, self.core_origin + "/actuator/health")
        self.forced_body_drain()
        require(self.sql("SELECT count(*) FROM trading_idempotency;") == "0" and
                self.sql("SELECT count(*) FROM trading_import_sessions;") == "0", "TRANSPORT_CREATED_DURABLE_WORK")
        self.evidence["durableAdmissionRowsCreated"] = False
        self.evidence["transportFaults"] = [fault.observation() for fault in self.body_faults.records]

    def entered(self, fault):
        require(fault.entered.wait(timeout=5) and fault.started, "BODY_FAULT_NOT_ENTERED")
        require(not fault.io_failure and not fault.guard_expired, "BODY_FAULT_IO_FAILED")

    def peer_cancelled(self, fault):
        require(fault.peer_closed.wait(timeout=5) and fault.peer_closed_before_release and not fault.release.is_set() and
                not fault.guard_expired and not fault.io_failure, "CORE_DID_NOT_CLOSE_BODY_TRANSPORT")
        require(fault.finished.wait(timeout=2), "BODY_HANDLER_NOT_FINISHED")

    def recovered(self):
        require(self.request("/trading-accounts/" + ACCOUNT).json()["id"] == ACCOUNT, "BODY_FAULT_SLOT_NOT_REUSABLE")

    def deadline_and_slot(self):
        fault = self.body_faults.arm("partial")
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(self.request, "/trading-accounts/" + ACCOUNT, 504, "REQUEST_DEADLINE_EXCEEDED")
            try:
                self.entered(fault)
                require(fault.prefix_sent, "PARTIAL_BODY_NOT_SENT")
                before = self.metadata_relay.counts["forwarded"]
                self.request("/trading-books/" + BOOK, 503, "OWNER_QUERY_UNAVAILABLE")
                require(self.metadata_relay.counts["forwarded"] == before, "EXHAUSTED_CORE_SLOT_REACHED_GO")
                pending.result(timeout=8)
                self.peer_cancelled(fault)
                self.recovered()
                self.evidence["partialBodyDeadlineElapsedSeconds"] = round(time.monotonic() - started, 2)
                self.mark("partial_body_uses_whole_core_deadline_cancels_wire_and_releases_single_slot")
            finally:
                fault.release.set()

    def rejected_body(self, kind):
        fault = self.body_faults.arm(kind)
        try:
            self.request("/trading-books/" + BOOK, 502, "UPSTREAM_SERVICE_ERROR")
            self.entered(fault)
            if kind != "truncate":
                self.peer_cancelled(fault)
            else:
                require(fault.prefix_sent and fault.finished.wait(timeout=2), "TRUNCATION_NOT_ESTABLISHED")
            self.recovered()
            self.mark(kind + "_maps_to_502_without_fallback_and_releases_slot")
        finally:
            fault.release.set()

    def browser_abort(self):
        fault = self.body_faults.arm("partial")
        context = ssl.create_default_context(cafile=str(self.directory / "root-ca.pem"))
        connection = context.wrap_socket(socket.create_connection(("127.0.0.1", self.core_port), timeout=3), server_hostname="127.0.0.1")
        try:
            message = f"GET /trading-accounts/{ACCOUNT} HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: REFINVEST_ACCESS_TOKEN={self.browser_token()}\r\ntraceparent: {TRACE_PARENT}\r\nConnection: close\r\n\r\n"
            connection.sendall(message.encode("ascii"))
            self.entered(fault)
            connection.close()
            self.peer_cancelled(fault)
            self.recovered()
            self.mark("browser_disconnect_bounds_body_wait_and_returns_slot_without_job_cancellation")
        finally:
            connection.close()
            fault.release.set()

    def core_log(self):
        name = next(p["name"] for p in self.evidence["processes"] if p["pid"] == self.core_process.pid)
        return self.directory / (name + ".log")

    def confirm_core_exit(self):
        require(self.core_process.wait(timeout=15) == 0, "BODY_DRAIN_CORE_EXIT")
        self.await_sql(f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{self.schema}-core';",
                       "0", "BODY_DRAIN_PRIMARY_SESSIONS_REMAIN")

    def healthy_body_drain(self):
        self.checkpoint("HEALTHY_BODY_DRAIN_START")
        fault = self.body_faults.arm("partial")
        log = self.core_log()
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(self.request, "/trading-accounts/" + ACCOUNT)
            try:
                self.entered(fault)
                self.checkpoint("HEALTHY_BODY_DRAIN_HELD")
                self.request("/trading-books/" + BOOK, 503, "OWNER_QUERY_UNAVAILABLE")
                self.checkpoint("HEALTHY_BODY_DRAIN_SHUTDOWN")
                self.trigger_shutdown()
                self.checkpoint("HEALTHY_BODY_DRAIN_SIGNALLED")
                deadline = time.monotonic() + 1.5
                while "Owner query draining" not in log.read_text(encoding="utf-8", errors="replace") and time.monotonic() < deadline:
                    time.sleep(0.025)
                require("Owner query draining" in log.read_text(encoding="utf-8", errors="replace") and not pending.done(),
                        "BODY_NOT_PENDING_DURING_ACTUAL_CORE_DRAIN")
                self.evidence["healthyBodyPendingDuringDrain"] = True
                fault.release.set()
                pending.result(timeout=8)
                require(fault.finished.wait(timeout=2), "HEALTHY_BODY_HANDLER_REMAINED")
                self.confirm_core_exit()
            finally:
                fault.release.set()
        text = log.read_text(encoding="utf-8", errors="replace")
        order = [text.find(marker) for marker in ("Owner query shutdown complete", "Commencing graceful shutdown", "Shutdown initiated")]
        require(all(n >= 0 for n in order) and order == sorted(order) and "Owner query shutdown incomplete" not in text,
                "HEALTHY_BODY_DRAIN_ORDER")
        self.mark("completed_body_drains_before_core_connector_and_primary_pool_close")

    def forced_body_drain(self):
        fault = self.body_faults.arm("partial")
        log = self.core_log()
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(self.request, "/trading-accounts/" + ACCOUNT, 503, "OWNER_QUERY_UNAVAILABLE")
            try:
                self.entered(fault)
                self.trigger_shutdown()
                pending.result(timeout=8)
                self.peer_cancelled(fault)
                self.confirm_core_exit()
            finally:
                fault.release.set()
        text = log.read_text(encoding="utf-8", errors="replace")
        require("Owner query shutdown incomplete" in text and "Owner query shutdown complete" not in text,
                "FORCED_BODY_DRAIN_NOT_REPORTED_INCOMPLETE")
        self.mark("forced_body_drain_cancels_physical_transport_and_reports_incomplete_even_with_exit_zero")

    def cleanup(self):
        try:
            if self.body_faults:
                self.body_faults.close()
            if self.metadata_relay:
                self.metadata_relay.close()
                self.evidence["metadataRelayStopped"] = True
        finally:
            super().cleanup()
