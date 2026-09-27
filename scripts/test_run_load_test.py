"""Failure accounting tests for the bounded HTTP load runner."""

import importlib.util
import json
import sys
import threading
import time
import tempfile
import os
import hashlib
import subprocess
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("run_load_test", Path(__file__).with_name("run-load-test.py"))
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


class FakeCore(BaseHTTPRequestHandler):
    mode = "completed"
    posts = 0
    gets = 0

    def log_message(self, *args):
        pass

    def do_POST(self):
        FakeCore.posts += 1
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if FakeCore.mode == "auth":
            self.send_response(401)
            self.end_headers()
            return
        if FakeCore.mode == "concurrency":
            payload = b'{"code":"BACKTEST_CONCURRENCY_LIMIT_EXCEEDED","message":"ignored"}'
            self.send_response(429)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if FakeCore.mode == "lifecycle" and self.path.endswith("/backtests") and FakeCore.posts >= 5:
            payload = b'{"code":"BACKTEST_CONCURRENCY_LIMIT_EXCEEDED"}'
            self.send_response(429)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if FakeCore.mode == "lifecycle" and self.path == "/strategies":
            payload = json.dumps({"id": "strategy-1"}).encode()
        elif FakeCore.mode == "lifecycle" and self.path.endswith("/versions"):
            payload = json.dumps({"id": "version-1"}).encode()
        elif FakeCore.mode == "lifecycle":
            payload = json.dumps({"id": str(FakeCore.posts)}).encode()
        else:
            payload = json.dumps({"id": "run-1"}).encode()
        self.send_response(201 if FakeCore.mode == "lifecycle" and not self.path.endswith("/backtests") else 202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        FakeCore.gets += 1
        if self.path == "/actuator/prometheus":
            self.send_response(401)
            self.end_headers()
            return
        if self.path in ("/actuator/health", "/health"):
            payload = b'{"status":"UP"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        status = "FAILED" if FakeCore.mode == "failed" else "RUNNING" if FakeCore.mode == "timeout" else "COMPLETED"
        payload = json.dumps({"status": status, "errorCode": "DSL_INVALID" if status == "FAILED" else None,
                              "datasetSnapshotId": runner.SNAPSHOT_ID}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class LoadAccountingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCore)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        FakeCore.mode = "completed"
        FakeCore.posts = 0
        FakeCore.gets = 0

    def test_auth_rejection_is_counted_and_never_polled(self):
        FakeCore.mode = "auth"
        phase = runner.run_phase("steady", self.url, "version", "test-token", 1, 1, 1, 1, 5, 0.2)
        self.assertEqual(phase["outcomes"]["submission_failed"], 1)
        self.assertEqual(phase["outcomes"]["rejected"], 0)
        self.assertEqual(phase["http_statuses"], {"401": 1})
        self.assertEqual(phase["outcomes"]["completed"], 0)
        self.assertEqual(FakeCore.gets, 0)

    def test_compute_failure_is_terminal_but_not_success(self):
        FakeCore.mode = "failed"
        phase = runner.run_phase("burst", self.url, "version", "test-token", 1, 1, 1, 1, 5, 0.2)
        self.assertEqual(phase["outcomes"]["failed"], 1)
        self.assertEqual(phase["records"][0]["error_code"], "DSL_INVALID")
        self.assertEqual(phase["terminal_latency"]["count"], 1)
        self.assertEqual(phase["outcomes"]["completed"], 0)

    def test_bounded_429_error_code_is_counted_without_message(self):
        FakeCore.mode = "concurrency"
        phase = runner.run_phase("burst", self.url, "version", "test-token", 1, 1, 1, 1, 5, 0.2)
        self.assertEqual(phase["outcomes"]["rejected"], 1)
        self.assertEqual(phase["http_error_codes"], {"BACKTEST_CONCURRENCY_LIMIT_EXCEEDED": 1})
        self.assertEqual(phase["accepted_work_terminal_latency"]["count"], 0)
        self.assertEqual(phase["rejection_response_latency"]["count"], 1)
        self.assertNotIn("ignored", json.dumps(phase))

    def test_terminal_timeout_is_censored(self):
        FakeCore.mode = "timeout"
        record = runner.submit(self.url, "version", "test-token", 0, time.monotonic())
        result = runner.poll_one(self.url, "test-token", record, 0.05, 0.01)
        self.assertEqual(result["outcome"], "terminal_timeout")
        self.assertIsNone(result["terminal_ms"])

    def test_duplicate_run_id_does_not_create_a_second_poll(self):
        phase = runner.run_phase("burst", self.url, "version", "test-token", 2, 1, 2, 2, 5, 0.2)
        self.assertEqual(phase["outcomes"]["duplicate_run_id"], 1)
        self.assertEqual(FakeCore.posts, 2)
        self.assertEqual(FakeCore.gets, 1)

    def test_down_core_target_masks_core_metrics(self):
        def fake_http(url, **kwargs):
            return 200, {"status": "success", "data": {"result": [
                {"metric": {"job": "core-api"}, "value": [1, "0"]}]}}, None
        with patch.object(runner, "http_json", side_effect=fake_http):
            summary = runner.prometheus_summary(self.url, 0, 30)
        self.assertEqual(summary["values"]["core_compute_p95_s"]["state"], "N/A")
        self.assertEqual(summary["values"]["core_submissions"]["state"], "N/A")
        self.assertEqual(summary["values"]["core_http_requests"]["state"], "N/A")

    def test_observability_preflight_does_not_treat_down_or_missing_as_zero(self):
        def fake_http(url, **kwargs):
            expression = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("query", [""])[0]
            if expression.startswith("up{"):
                result = [
                    {"metric": {"job": "core-api"}, "value": [1, "0"]},
                    {"metric": {"job": "compute-api"}, "value": [1, "1"]},
                    {"metric": {"job": "compute-worker"}, "value": [1, "1"]},
                ]
            else:
                result = []
            return 200, {"status": "success", "data": {"result": result}}, None
        with patch.object(runner, "http_json", side_effect=fake_http):
            result = runner.prometheus_preflight(self.url, attempts=1, interval=0)
        self.assertEqual(result["state"], "not_ready")
        self.assertEqual(result["targets"]["core-api"], "0")
        self.assertTrue(all(value["state"] == "N/A" for value in result["required_series"].values()))

    def test_database_mismatch_is_not_verified(self):
        phases = [{"records": [{"accepted": True, "run_id": "123", "terminal_status": "COMPLETED",
                                "error_code": None}]}]
        fake = subprocess.CompletedProcess(
            [], 0,
            stdout="123|FAILED|DSL_INVALID|2026-09-27 00:00:00+00|TERMINAL|123|"
                   "2026-09-27 00:00:00+00|2026-09-27 00:00:02+00|FAILED|"
                   "2026-09-27 00:00:00+00|2026-09-27 00:00:01+00|\n",
        )
        values = {"REFINVEST_LOAD_PSQL": "psql", "REFINVEST_LOAD_DB_HOST": "127.0.0.1",
                  "REFINVEST_LOAD_DB_PORT": "5433", "REFINVEST_LOAD_DB_USER": "refinvest"}
        with patch.dict(os.environ, values), patch.object(runner.subprocess, "run", return_value=fake) as run:
            result = runner.verify_database(phases)
        self.assertEqual(result["state"], "mismatch")
        self.assertEqual(result["mismatch_run_ids"], ["123"])
        self.assertEqual(result["stage_boundary_evidence"]["123"]["compute_status"], "FAILED")
        self.assertIn("d.compute_run_id::uuid", run.call_args.args[0][-1])

    def test_repetition_analysis_requires_consistent_stage_evidence(self):
        def phase(repetition, terminal, queue_sum, attempt_sum):
            return {
                "scenario_kind": "processing", "repetition": repetition,
                "accepted_work_terminal_latency": {"median_ms": terminal},
                "direct_metrics": {"compute_worker": {"state": "observed", "deltas": {
                    "refinvest_compute_worker_queue_wait_seconds_sum": queue_sum,
                    "refinvest_compute_worker_queue_wait_seconds_count": 1,
                    "refinvest_compute_worker_attempt_duration_seconds_sum": attempt_sum,
                    "refinvest_compute_worker_attempt_duration_seconds_count": 1,
                }}},
            }
        result = runner.analyze_repetitions([
            phase(1, 100, 1, 2), phase(2, 200, 2, 2), phase(3, 300, 3, 2),
            {"scenario_kind": "saturation", "outcomes": {"rejected": 16}},
        ])
        self.assertEqual(result["state"], "candidate")
        self.assertEqual(result["candidate"], "Compute worker capacity / queueing")
        self.assertTrue(result["saturation"]["excluded_from_work_latency"])

    def test_repetition_analysis_revises_hypothesis_to_core_poll_scheduling(self):
        phases = []
        for repetition in range(1, 4):
            phases.append({
                "scenario_kind": "processing", "repetition": repetition,
                "accepted_work_terminal_latency": {"median_ms": 10_000},
                "direct_metrics": {"compute_worker": {"state": "observed", "deltas": {
                    "refinvest_compute_worker_queue_wait_seconds_sum": 1,
                    "refinvest_compute_worker_queue_wait_seconds_count": 1,
                    "refinvest_compute_worker_attempt_duration_seconds_sum": 1,
                    "refinvest_compute_worker_attempt_duration_seconds_count": 1,
                }}},
                "prometheus": {"values": {
                    "core_scheduler_p95_s": {"series": [{"labels": {"operation": "poll"}, "value": "5"}]},
                    "core_compute_p95_s": {"series": [{"labels": {"operation": "poll"}, "value": "0.2"}]},
                }},
                "approximate_compute_terminal_to_core_terminal": {"median_ms": 4_000},
            })
        result = runner.analyze_repetitions(phases)
        self.assertEqual(result["state"], "candidate")
        self.assertIn("Core durable poll scheduling", result["candidate"])
        self.assertIn("not terminal DB write", result["candidate"])

    def test_full_report_counts_match_records_and_preserves_scrape_failure(self):
        FakeCore.mode = "lifecycle"
        with tempfile.TemporaryDirectory() as output, patch.dict(os.environ, {"REFINVEST_LOAD_ACCESS_TOKEN": "test-token"}), \
                patch.object(runner, "verify_database", return_value={"state": "verified"}), \
                patch.object(runner, "log_match_counts", return_value={}), \
                patch.object(runner, "direct_snapshot", return_value={"compute_api": {"state": "observed", "samples": {}},
                                                                   "compute_worker": {"state": "observed", "samples": {}}}), \
                patch.object(runner, "prometheus_preflight", return_value={"state": "ready"}), \
                patch.object(runner, "prometheus_summary", return_value={"values": {}}):
            fixture = Path(output) / "fixture"
            fixture.mkdir()
            artifacts = {}
            for key, filename in (("calendar", "calendar.json"), ("dailyBars", "daily-bars.ndjson")):
                content = b"test fixture"
                (fixture / filename).write_bytes(content)
                artifacts[key] = {"path": filename, "sha256": hashlib.sha256(content).hexdigest()}
            (fixture / "manifest.json").write_text(json.dumps({"snapshotId": runner.SNAPSHOT_ID, **artifacts}))
            code = runner.main(["--core-url", self.url, "--compute-url", self.url,
                                "--fixture-root", str(fixture), "--output-root", output,
                                "--steady-rate", "1", "--steady-duration", "1",
                                "--burst-rate", "1", "--burst-duration", "1",
                                "--max-submissions", "1", "--cooldown", "0",
                                "--repetitions", "1",
                                "--terminal-timeout", "5"])
            reports = list(Path(output).glob("*/result.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(code, 0, report.get("error"))
            serialized = json.dumps(report)
            self.assertNotIn("test-token", serialized)
            self.assertNotIn("XSRF-TOKEN", serialized)
            self.assertNotIn("primarySignalAsset", serialized)
            self.assertEqual(report["core_scrape"]["state"], "HTTP_401")
            self.assertEqual(report["observability_status"], "partial")
            self.assertEqual(report["status"], "passed")
            for phase in report["phases"]:
                self.assertEqual(phase["submitted"], len(phase["records"]))
                self.assertEqual(phase["accepted"], sum(record["accepted"] for record in phase["records"]))
                self.assertEqual(sum(phase["outcomes"].values()), phase["submitted"])
            self.assertTrue(reports[0].with_name("summary.md").exists())


if __name__ == "__main__":
    unittest.main()
