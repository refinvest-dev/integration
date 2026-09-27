"""Bounded, real-HTTP local Core-to-Compute load exploration (stdlib only)."""

from __future__ import annotations

import argparse
import concurrent.futures
import email.utils
import hashlib
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_ID = "integration-e2e-v1"
TERMINAL = {"COMPLETED", "FAILED"}
SAFE_HTTP_ERROR_CODES = {"BACKTEST_CONCURRENCY_LIMIT_EXCEEDED", "BACKTEST_MONTHLY_LIMIT_EXCEEDED"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def revision(path: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "N/A"


def distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "median_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3),
        "max_ms": round(ordered[-1], 3),
    }


def http_json(url: str, method: str = "GET", body: dict | None = None, token: str | None = None,
              timeout: float = 10, expect_json: bool = True) -> tuple[int | None, dict | None, str | None]:
    headers = {"Accept": "application/json"}
    data = None
    if token is not None:
        headers["Cookie"] = f"REFINVEST_ACCESS_TOKEN={token}; XSRF-TOKEN=integration-load-csrf"
        headers["X-XSRF-TOKEN"] = "integration-load-csrf"
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with OPENER.open(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
            return response.status, json.loads(raw) if raw and expect_json else None, None
    except urllib.error.HTTPError as error:
        code = "http_error"
        try:
            response = json.loads(error.read(4096))
            if isinstance(response, dict) and response.get("code") in SAFE_HTTP_ERROR_CODES:
                code = response["code"]
        except (ValueError, UnicodeError):
            pass
        error.close()
        return error.code, None, code
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
            return None, None, "TimeoutError"
        return None, None, type(error).__name__
    except (ValueError, UnicodeError):
        return None, None, "invalid_json"


def fixture_identity(root: Path) -> dict:
    manifest_path = root / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("snapshotId") != SNAPSHOT_ID:
        raise ValueError("Fixture snapshot ID does not match integration-e2e-v1")
    artifacts = {}
    for key in ("calendar", "dailyBars"):
        item = manifest[key]
        artifact = (root / item["path"]).resolve()
        if root.resolve() not in artifact.parents:
            raise ValueError("Fixture artifact path escapes its root")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise ValueError(f"Fixture {key} hash mismatch")
        artifacts[key] = digest
    return {"snapshot_id": SNAPSHOT_ID, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "artifact_sha256": artifacts}


def probe_core_scrape(core: str) -> dict:
    status, _, error = http_json(core + "/actuator/prometheus", timeout=3, expect_json=False)
    if status == 200:
        return {"state": "HTTP_200", "note": "인증된 Prometheus scrape 성공 여부는 별도 확인 필요"}
    if status == 401:
        return {"state": "HTTP_401", "note": "Core metric 기반 결론 불가"}
    return {"state": f"HTTP_{status}" if status is not None else "UNAVAILABLE", "error": error}


def clock_skew_probe(url: str) -> dict:
    """Estimate remote HTTP wall-clock offset; never use it as a stage duration."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    before = time.time()
    try:
        with OPENER.open(request, timeout=3) as response:
            response.read(4096)
            date_header = response.headers.get("Date")
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"state": "N/A", "reason": "health clock probe failed"}
    after = time.time()
    if not date_header:
        return {"state": "N/A", "reason": "HTTP Date header absent"}
    try:
        remote = email.utils.parsedate_to_datetime(date_header).timestamp()
    except (TypeError, ValueError, OverflowError):
        return {"state": "N/A", "reason": "invalid HTTP Date header"}
    return {"state": "estimated", "offset_ms": round((remote - ((before + after) / 2)) * 1000, 3),
            "round_trip_ms": round((after - before) * 1000, 3),
            "resolution": "HTTP Date is second-resolution",
            "use": "display only; cross-process timestamps are not subtracted as exact durations"}


def create_version(core: str, token: str) -> str:
    strategy = {"name": "Integration load " + uuid.uuid4().hex[:12]}
    status, data, _ = http_json(core + "/strategies", "POST", strategy, token)
    if status not in (200, 201) or not data or not data.get("id"):
        raise RuntimeError(f"Strategy preparation failed: HTTP {status}")
    version = {
        "primarySignalAsset": "QQQ", "executionAsset": "QQQ", "lag": 0,
        "conditions": [{"operator": "GT", "logicalCombinator": None,
                        "operandA": {"asset": "QQQ", "metric": "SIMPLE", "window": None},
                        "operandB": 1000}],
        "exit": {"holdingSignalSessions": 1},
    }
    status, data, _ = http_json(core + f"/strategies/{data['id']}/versions", "POST", version, token)
    if status not in (200, 201) or not data or not data.get("id"):
        raise RuntimeError(f"StrategyVersion preparation failed: HTTP {status}")
    return str(data["id"])


def submit(core: str, version_id: str, token: str, index: int, planned: float) -> dict:
    started = time.monotonic()
    started_unix = time.time()
    body = {"period": {"start": "2025-01-02", "end": "2025-01-08"},
            "feeModel": {"commission": 0, "slippage": 0}}
    status, data, error = http_json(core + f"/strategy-versions/{version_id}/backtests", "POST", body, token)
    ended = time.monotonic()
    run_id = str(data["id"]) if isinstance(data, dict) and data.get("id") is not None else None
    accepted = status in (200, 201, 202) and run_id is not None
    if accepted:
        outcome = "accepted"
    elif error in SAFE_HTTP_ERROR_CODES:
        outcome = "rejected"
    elif error == "TimeoutError":
        outcome = "submission_timeout"
    else:
        outcome = "submission_failed"
    return {"index": index, "schedule_lag_ms": round((started - planned) * 1000, 3),
            "started_utc": datetime.fromtimestamp(started_unix, timezone.utc).isoformat(),
            "submitted_monotonic": ended, "http_status": status, "client_error": error,
            "submit_ms": round((ended - started) * 1000, 3), "accepted": accepted,
            "run_id": run_id, "terminal_status": None, "error_code": None,
            "terminal_ms": None, "poll_count": 0, "outcome": outcome,
            "stage_boundaries": {
                "client_submission_started": "observed_monotonic",
                "core_receipt_and_persistence": "not_individually_observed",
                "compute_acceptance": "not_individually_observed",
                "worker_queue_wait": "aggregate_metric_only",
                "worker_execution": "aggregate_metric_only",
                "compute_terminal_commit": "aggregate_metric_only",
                "core_poll_observation": "aggregate_metric_only",
                "core_terminal_persistence": "aggregate_commit_metric_and_db_state",
                "client_terminal_observation": "observed_monotonic" if accepted else "not_applicable",
            }}


def poll_one(core: str, token: str, record: dict, terminal_timeout: float, interval: float) -> dict:
    deadline = record["submitted_monotonic"] + terminal_timeout
    while True:
        status, data, error = http_json(core + f"/backtest-runs/{record['run_id']}", token=token)
        record["poll_count"] += 1
        if status != 200 or not isinstance(data, dict):
            record["outcome"] = "poll_error"
            record["poll_http_status"] = status
            record["poll_client_error"] = error
            return record
        state = data.get("status")
        if state in TERMINAL:
            record["terminal_status"] = state
            record["error_code"] = data.get("errorCode") if state == "FAILED" else None
            record["terminal_ms"] = round((time.monotonic() - record["submitted_monotonic"]) * 1000 + record["submit_ms"], 3)
            record["outcome"] = "completed" if state == "COMPLETED" else "failed"
            if state == "COMPLETED" and data.get("datasetSnapshotId") != SNAPSHOT_ID:
                record["outcome"] = "fixture_mismatch"
            return record
        if state not in ("PENDING", "RUNNING"):
            record["outcome"] = "invalid_status"
            return record
        if time.monotonic() >= deadline:
            record["outcome"] = "terminal_timeout"
            return record
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def run_phase(name: str, core: str, version_id: str, token: str, rate: float, duration: float,
              max_submissions: int, concurrency: int, terminal_timeout: float, poll_interval: float,
              repetition: int = 1, scenario_kind: str = "processing") -> dict:
    phase_start = time.monotonic()
    phase_start_utc = utc_now()
    futures = []
    poll_futures = []
    seen = set()
    active = 0
    max_active = 0
    from threading import Lock
    lock = Lock()

    def tracked_submit(index: int, planned: float) -> dict:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            record = submit(core, version_id, token, index, planned)
            if record["accepted"]:
                with lock:
                    if record["run_id"] in seen:
                        record["outcome"] = "duplicate_run_id"
                    else:
                        seen.add(record["run_id"])
                        poll_futures.append(poll_pool.submit(
                            poll_one, core, token, record, terminal_timeout, poll_interval))
            return record
        finally:
            with lock:
                active -= 1

    poll_workers = min(max_submissions, max(4, concurrency * 2))
    with concurrent.futures.ThreadPoolExecutor(max_workers=poll_workers) as poll_pool:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            for index in range(max_submissions):
                planned = phase_start + index / rate
                if planned - phase_start >= duration:
                    break
                delay = planned - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                futures.append(pool.submit(tracked_submit, index, planned))
            records = [future.result() for future in futures]
        submission_end = time.monotonic()
        for future in poll_futures:
            future.result()
    accepted = [record for record in records if record["accepted"]]
    end = time.monotonic()
    outcomes = {key: sum(record["outcome"] == key for record in records)
                for key in ("completed", "failed", "rejected", "submission_failed", "submission_timeout",
                            "poll_error", "terminal_timeout",
                            "invalid_status", "fixture_mismatch", "duplicate_run_id")}
    http_codes = {}
    http_error_codes = {}
    for record in records:
        key = str(record["http_status"]) if record["http_status"] is not None else "client_error"
        http_codes[key] = http_codes.get(key, 0) + 1
        if record["client_error"]:
            error_code = record["client_error"]
            http_error_codes[error_code] = http_error_codes.get(error_code, 0) + 1
    accepted_terminal = [r["terminal_ms"] for r in accepted if r["terminal_ms"] is not None]
    rejected = [r for r in records if not r["accepted"]]
    counts = {"requested": len(records), "accepted": len(accepted),
              "rejected": outcomes["rejected"], "completed": outcomes["completed"],
              "failed": outcomes["failed"] + outcomes["submission_failed"] + outcomes["poll_error"]
                        + outcomes["invalid_status"] + outcomes["fixture_mismatch"] + outcomes["duplicate_run_id"],
              "timeout": outcomes["submission_timeout"] + outcomes["terminal_timeout"]}
    return {"name": name, "scenario_kind": scenario_kind, "repetition": repetition,
            "started_utc": phase_start_utc, "ended_utc": utc_now(),
            "planned_rate_per_s": rate, "duration_s": duration, "max_submissions": max_submissions,
            "submission_concurrency": concurrency, "terminal_timeout_s": terminal_timeout,
            "poll_interval_s": poll_interval, "submitted": len(records), "accepted": len(accepted),
            "counts": counts, "outcomes": outcomes, "http_statuses": http_codes, "http_error_codes": http_error_codes,
            "actual_submit_rate_per_s": round(len(records) / max(submission_end - phase_start, len(records) / rate), 3),
            "max_concurrent_submissions": max_active,
            "poll_concurrency": poll_workers,
            "submit_window_s": round(submission_end - phase_start, 3), "drain_s": round(end - submission_end, 3),
            "submit_latency": distribution([r["submit_ms"] for r in records]),
            "accepted_submit_latency": distribution([r["submit_ms"] for r in accepted]),
            "rejection_response_latency": distribution([r["submit_ms"] for r in rejected]),
            "accepted_work_terminal_latency": distribution(accepted_terminal),
            "terminal_latency": distribution(accepted_terminal),
            "terminal_latency_censored": sum(r["accepted"] and r["terminal_ms"] is None for r in records),
            "poll_requests": sum(r["poll_count"] for r in records), "records": records}


def prometheus_summary(base: str, start: float, end: float) -> dict:
    window = max(15, math.ceil(end - start))
    expressions = {
        "targets": 'up{job=~"core-api|compute-api|compute-worker"}',
        "compute_accepted": f'increase(refinvest_compute_backtest_jobs_accepted_total{{job="compute-api"}}[{window}s])',
        "compute_capacity_rejections": f'increase(refinvest_compute_backtest_capacity_rejections_total{{job="compute-api"}}[{window}s])',
        "worker_claims": f'increase(refinvest_compute_worker_claims_total{{job="compute-worker"}}[{window}s])',
        "worker_terminal": f'increase(refinvest_compute_worker_terminal_transitions_total{{job="compute-worker"}}[{window}s])',
        "worker_failures": f'increase(refinvest_compute_worker_failures_total{{job="compute-worker"}}[{window}s])',
        "worker_retries": f'increase(refinvest_compute_worker_retries_scheduled_total{{job="compute-worker"}}[{window}s])',
        "worker_queue_p95_s": f'histogram_quantile(0.95, sum by (le) (rate(refinvest_compute_worker_queue_wait_seconds_bucket{{job="compute-worker"}}[{window}s])))',
        "worker_queue_samples": f'increase(refinvest_compute_worker_queue_wait_seconds_count{{job="compute-worker"}}[{window}s])',
        "worker_attempt_p95_s": f'histogram_quantile(0.95, sum by (le) (rate(refinvest_compute_worker_attempt_duration_seconds_bucket{{job="compute-worker"}}[{window}s])))',
        "worker_attempt_samples": f'increase(refinvest_compute_worker_attempt_duration_seconds_count{{job="compute-worker"}}[{window}s])',
        "core_compute_p95_s": f'histogram_quantile(0.95, sum by (le,operation) (rate(refinvest_core_compute_client_seconds_bucket{{job="core-api"}}[{window}s])))',
        "core_compute_samples": f'increase(refinvest_core_compute_client_seconds_count{{job="core-api"}}[{window}s])',
        "core_submissions": f'increase(refinvest_backtest_submissions_total{{job="core-api"}}[{window}s])',
        "core_http_requests": f'sum(increase(http_server_requests_seconds_count{{job="core-api"}}[{window}s]))',
        "core_scheduler_p95_s": f'histogram_quantile(0.95, sum by (le,operation) (rate(refinvest_core_backtest_scheduler_lag_seconds_bucket{{job="core-api"}}[{window}s])))',
        "core_terminal": f'increase(refinvest_backtest_terminal_total{{job="core-api"}}[{window}s])',
        "core_terminal_persistence_failures": f'increase(refinvest_core_backtest_terminal_persistence_failures_total{{job="core-api"}}[{window}s])',
        "core_retries": f'increase(refinvest_backtest_retries_total{{job="core-api"}}[{window}s])',
    }
    result = {"window_seconds": window, "queried_at_utc": utc_now(), "values": {}}
    ready_status, _, ready_error = http_json(base + "/-/ready", timeout=3, expect_json=False)
    if ready_status != 200:
        reason = ready_error or f"HTTP {ready_status}"
        result["preflight"] = {"state": "N/A", "reason": reason}
        result["values"] = {name: {"state": "N/A", "reason": "Prometheus unavailable: " + reason}
                            for name in expressions}
        return result
    for name, expression in expressions.items():
        query = urllib.parse.urlencode({"query": expression, "time": end})
        status, data, error = http_json(base + "/api/v1/query?" + query, timeout=3)
        if status != 200 or not data or data.get("status") != "success":
            result["values"][name] = {"state": "N/A", "reason": error or f"HTTP {status}"}
            continue
        series = data.get("data", {}).get("result", [])
        result["values"][name] = {"state": "observed" if series else "N/A",
                                  "series": [{"labels": item.get("metric", {}), "value": item.get("value", [None, None])[1]}
                                             for item in series]}
    targets = result["values"]["targets"]
    core_up = any(item["labels"].get("job") == "core-api" and item["value"] == "1"
                  for item in targets.get("series", []))
    if not core_up:
        for name in ("core_compute_p95_s", "core_compute_samples", "core_submissions",
                     "core_http_requests", "core_scheduler_p95_s", "core_terminal",
                     "core_terminal_persistence_failures", "core_retries"):
            result["values"][name] = {"state": "N/A", "reason": "Core target DOWN or unavailable"}
    return result


def verify_database(phases: list[dict]) -> dict:
    executable = os.environ.get("REFINVEST_LOAD_PSQL")
    if not executable:
        return {"state": "N/A", "reason": "psql was not configured"}
    ids = [record["run_id"] for phase in phases for record in phase["records"] if record["accepted"]]
    if not ids:
        return {"state": "N/A", "reason": "No accepted run IDs"}
    if any(not value.isdecimal() for value in ids):
        return {"state": "error", "reason": "Non-numeric run ID"}
    command = [executable, "-h", os.environ["REFINVEST_LOAD_DB_HOST"], "-p", os.environ["REFINVEST_LOAD_DB_PORT"],
               "-U", os.environ["REFINVEST_LOAD_DB_USER"], "-d", "refinvest_integration",
               "-v", "ON_ERROR_STOP=1", "-At", "-F", "|", "-c",
               "SELECT br.id,br.status,COALESCE(br.error_code,''),br.created_at," +
               "COALESCE(d.status::text,''),COALESCE(d.compute_run_id,''),d.created_at,d.updated_at," +
               "COALESCE(cj.status,''),cj.created_at,cj.terminal_at,cj.completed_at " +
               "FROM backtest_runs br LEFT JOIN backtest_compute_dispatches d ON d.backtest_run_id=br.id " +
               "LEFT JOIN compute_backtest_jobs cj ON cj.run_id=d.compute_run_id::uuid " +
               "WHERE br.id IN (" + ",".join(ids) + ")"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError):
        return {"state": "error", "reason": "Read-only integration DB query failed"}
    found = {}
    for line in completed.stdout.splitlines():
        parts = line.split("|")
        if len(parts) == 12:
            found[parts[0]] = {
                "status": parts[1], "error_code": parts[2] or None,
                "boundaries": {
                    "core_submission_persisted_at": parts[3] or None,
                    "core_dispatch_status": parts[4] or None,
                    "compute_run_id": parts[5] or None,
                    "core_dispatch_created_at": parts[6] or None,
                    "core_dispatch_last_updated_at": parts[7] or None,
                    "compute_status": parts[8] or None,
                    "compute_acceptance_persisted_at": parts[9] or None,
                    "compute_terminal_persisted_at": parts[10] or None,
                    "compute_completed_at": parts[11] or None,
                },
            }
    mismatches = []
    for phase in phases:
        for record in phase["records"]:
            if not record["accepted"]:
                continue
            row = found.get(record["run_id"])
            if not row or (record["terminal_status"] and row["status"] != record["terminal_status"]):
                mismatches.append(record["run_id"])
            elif record["terminal_status"] == "FAILED" and row["error_code"] != record["error_code"]:
                mismatches.append(record["run_id"])
    return {"state": "verified" if not mismatches else "mismatch", "accepted_count": len(ids),
            "found_count": len(found), "mismatch_run_ids": mismatches,
            "status_counts": {state: sum(row["status"] == state for row in found.values())
                              for state in ("PENDING", "RUNNING", "COMPLETED", "FAILED")},
            "stage_boundary_evidence": {run_id: row["boundaries"] for run_id, row in found.items()},
            "timestamp_policy": "Raw wall-clock timestamps prove durable boundaries only. Cross-service differences are not exact durations."}


DIRECT_NAMES = {
    "refinvest_compute_backtest_jobs_accepted_total",
    "refinvest_compute_backtest_capacity_rejections_total",
    "refinvest_compute_worker_claims_total",
    "refinvest_compute_worker_queue_wait_seconds_count",
    "refinvest_compute_worker_queue_wait_seconds_sum",
    "refinvest_compute_worker_queue_wait_seconds_bucket",
    "refinvest_compute_worker_attempt_duration_seconds_count",
    "refinvest_compute_worker_attempt_duration_seconds_sum",
    "refinvest_compute_worker_attempt_duration_seconds_bucket",
    "refinvest_compute_worker_terminal_transitions_total",
    "refinvest_compute_worker_retries_scheduled_total",
    "refinvest_compute_worker_failures_total",
}


def direct_snapshot() -> dict:
    values = {}
    for owner, port in (("compute_api", 9101), ("compute_worker", 9102)):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3) as response:
                exposition = response.read(2_000_000).decode("utf-8")
        except (urllib.error.URLError, OSError, UnicodeError):
            values[owner] = {"state": "N/A"}
            continue
        samples = {}
        for line in exposition.splitlines():
            match = re.match(r'^([a-zA-Z_:][a-zA-Z0-9_:]*(?:\{[^}]*\})?)\s+([-+0-9.eE]+)$', line)
            if match and match.group(1).split("{", 1)[0] in DIRECT_NAMES:
                try:
                    samples[match.group(1)] = float(match.group(2))
                except ValueError:
                    continue
        values[owner] = {"state": "observed", "samples": samples}
    return values


def direct_delta(start: dict, end: dict) -> dict:
    result = {}
    for owner in ("compute_api", "compute_worker"):
        before, after = start[owner], end[owner]
        if before["state"] != "observed" or after["state"] != "observed":
            result[owner] = {"state": "N/A", "reason": "metrics endpoint unavailable"}
            continue
        deltas = {}
        for name, value in after["samples"].items():
            old = before["samples"].get(name)
            if old is not None and value >= old:
                deltas[name] = round(value - old, 6)
        result[owner] = {"state": "observed", "deltas": deltas,
                         "note": "process-local scrape difference; restart resets invalidate missing/decreased series"}
    return result


def prometheus_preflight(base: str, attempts: int = 6, interval: float = 5) -> dict:
    required = {
        "compute_acceptance": 'refinvest_compute_backtest_jobs_accepted_total{job="compute-api"}',
        "worker_queue": 'refinvest_compute_worker_queue_wait_seconds_count{job="compute-worker"}',
        "worker_attempt": 'refinvest_compute_worker_attempt_duration_seconds_count{job="compute-worker"}',
        "worker_terminal": 'refinvest_compute_worker_terminal_transitions_total{job="compute-worker"}',
        "core_submission": 'refinvest_backtest_submissions_total{job="core-api"}',
        "core_compute_submit": 'refinvest_core_compute_client_seconds_count{job="core-api",operation="submit"}',
        "core_compute_poll": 'refinvest_core_compute_client_seconds_count{job="core-api",operation="poll"}',
        "core_dispatch_lag": 'refinvest_core_backtest_scheduler_lag_seconds_count{job="core-api",operation="dispatch"}',
        "core_poll_lag": 'refinvest_core_backtest_scheduler_lag_seconds_count{job="core-api",operation="poll"}',
        "core_terminal_commit": 'refinvest_backtest_terminal_total{job="core-api"}',
    }
    last = {"state": "not_ready", "targets": {}, "required_series": {}}
    for attempt in range(1, attempts + 1):
        target_status, target_data, target_error = http_json(
            base + "/api/v1/query?" + urllib.parse.urlencode({"query": 'up{job=~"core-api|compute-api|compute-worker"}'}),
            timeout=3,
        )
        targets = {}
        if target_status == 200 and target_data and target_data.get("status") == "success":
            targets = {item.get("metric", {}).get("job"): item.get("value", [None, None])[1]
                       for item in target_data.get("data", {}).get("result", [])}
        series = {}
        for name, expression in required.items():
            status, data, error = http_json(
                base + "/api/v1/query?" + urllib.parse.urlencode({"query": expression}), timeout=3
            )
            observed = status == 200 and data and data.get("status") == "success" \
                and bool(data.get("data", {}).get("result", []))
            series[name] = {"state": "observed" if observed else "N/A",
                            "reason": None if observed else error or f"HTTP {status}"}
        targets_up = all(targets.get(job) == "1" for job in ("core-api", "compute-api", "compute-worker"))
        series_ready = all(value["state"] == "observed" for value in series.values())
        last = {"state": "ready" if targets_up and series_ready else "not_ready",
                "attempt": attempt, "targets": targets, "required_series": series,
                "target_query_error": target_error}
        if last["state"] == "ready":
            return last
        if attempt < attempts:
            time.sleep(interval)
    return last


def _delta_total(phase: dict, metric: str) -> float | None:
    worker = phase.get("direct_metrics", {}).get("compute_worker", {})
    if worker.get("state") != "observed":
        return None
    matches = [value for name, value in worker.get("deltas", {}).items()
               if name.split("{", 1)[0] == metric]
    return sum(matches) if matches else None


def _histogram_mean(phase: dict, metric: str) -> float | None:
    total = _delta_total(phase, metric + "_sum")
    count = _delta_total(phase, metric + "_count")
    return round(total / count, 6) if total is not None and count else None


def _histogram_quantile(phase: dict, metric: str, quantile: float) -> float | None:
    worker = phase.get("direct_metrics", {}).get("compute_worker", {})
    buckets = []
    for name, value in worker.get("deltas", {}).items():
        if name.split("{", 1)[0] != metric + "_bucket":
            continue
        match = re.search(r'(?:^|,)le="([^"]+)"', name.split("{", 1)[1].rstrip("}"))
        if not match or match.group(1) == "+Inf":
            continue
        buckets.append((float(match.group(1)), value))
    total = _delta_total(phase, metric + "_count")
    if not buckets or not total:
        return None
    target = total * quantile
    for boundary, cumulative in sorted(buckets):
        if cumulative >= target:
            return boundary
    return None


def _increases(values: list[float | None]) -> bool:
    return len(values) >= 3 and all(value is not None for value in values) and all(
        float(values[index]) <= float(values[index + 1]) for index in range(len(values) - 1)
    ) and float(values[-1]) > float(values[0])


def _prometheus_operation_value(phase: dict, metric: str, operation: str) -> float | None:
    for item in phase.get("prometheus", {}).get("values", {}).get(metric, {}).get("series", []):
        if item.get("labels", {}).get("operation") == operation:
            try:
                return round(float(item["value"]), 6)
            except (KeyError, TypeError, ValueError):
                return None
    return None


def cross_clock_stage_summary(phases: list[dict], database: dict, clock_skew: dict) -> dict:
    evidence = database.get("stage_boundary_evidence", {})
    repetitions = []
    for phase in phases:
        values = []
        for record in phase["records"]:
            if not record["accepted"]:
                continue
            boundary = evidence.get(record["run_id"], {})
            compute_terminal = boundary.get("compute_terminal_persisted_at")
            core_terminal = boundary.get("core_dispatch_last_updated_at")
            if not compute_terminal or not core_terminal:
                continue
            try:
                values.append((datetime.fromisoformat(core_terminal) -
                               datetime.fromisoformat(compute_terminal)).total_seconds() * 1000)
            except ValueError:
                continue
        summary = distribution(values)
        phase["approximate_compute_terminal_to_core_terminal"] = summary
        repetitions.append({"scenario_kind": phase["scenario_kind"], "repetition": phase["repetition"], **summary})
    return {
        "clock_skew": clock_skew,
        "repetitions": repetitions,
        "policy": "Cross-process wall-clock subtraction is approximate boundary evidence only, not an exact stage duration.",
        "core_boundary": "dispatch TERMINAL updated_at is in the same Core transaction after terminal run persistence; it is not a standalone persistence timer.",
    }


def analyze_repetitions(phases: list[dict]) -> dict:
    processing = [phase for phase in phases if phase["scenario_kind"] == "processing"]
    saturation = [phase for phase in phases if phase["scenario_kind"] == "saturation"]
    terminal_medians = [phase["accepted_work_terminal_latency"]["median_ms"] for phase in processing]
    queue_means = [_histogram_mean(phase, "refinvest_compute_worker_queue_wait_seconds")
                   for phase in processing]
    execution_means = [_histogram_mean(phase, "refinvest_compute_worker_attempt_duration_seconds")
                       for phase in processing]
    core_poll_lag_p95 = [_prometheus_operation_value(phase, "core_scheduler_p95_s", "poll")
                         for phase in processing]
    core_poll_http_p95 = [_prometheus_operation_value(phase, "core_compute_p95_s", "poll")
                          for phase in processing]
    cross_clock_medians = [phase.get("approximate_compute_terminal_to_core_terminal", {}).get("median_ms")
                           for phase in processing]
    consistent = {
        "accepted_work_terminal_median_ms": terminal_medians,
        "compute_queue_mean_s": queue_means,
        "worker_attempt_mean_s": execution_means,
        "compute_queue_median_s": [_histogram_quantile(phase, "refinvest_compute_worker_queue_wait_seconds", 0.5)
                                     for phase in processing],
        "compute_queue_p95_s": [_histogram_quantile(phase, "refinvest_compute_worker_queue_wait_seconds", 0.95)
                                  for phase in processing],
        "worker_attempt_median_s": [_histogram_quantile(phase, "refinvest_compute_worker_attempt_duration_seconds", 0.5)
                                     for phase in processing],
        "worker_attempt_p95_s": [_histogram_quantile(phase, "refinvest_compute_worker_attempt_duration_seconds", 0.95)
                                  for phase in processing],
        "terminal_consistently_increases": _increases(terminal_medians),
        "queue_consistently_increases": _increases(queue_means),
        "execution_consistently_increases": _increases(execution_means),
        "core_poll_scheduler_p95_s": core_poll_lag_p95,
        "core_poll_http_p95_s": core_poll_http_p95,
        "approximate_compute_terminal_to_core_terminal_median_ms": cross_clock_medians,
    }
    candidate = None
    if len(processing) >= 3 and all(value is not None for value in core_poll_lag_p95 + core_poll_http_p95
                                    + cross_clock_medians) and all(
        float(core_poll_lag_p95[index]) > float(core_poll_http_p95[index])
        for index in range(len(processing))
    ):
        candidate = "Core durable poll scheduling/cadence (not terminal DB write duration)"
    elif consistent["terminal_consistently_increases"] and consistent["queue_consistently_increases"] \
            and not consistent["execution_consistently_increases"]:
        candidate = "Compute worker capacity / queueing"
    elif consistent["terminal_consistently_increases"] and consistent["execution_consistently_increases"]:
        candidate = "Compute worker execution"
    if candidate:
        conclusion = {"state": "candidate", "candidate": candidate,
                      "evidence": consistent,
                      "caveat": "Candidate only. Cross-clock DB differences are approximate, and terminal persistence write duration is not independently instrumented."}
    else:
        conclusion = {"state": "indeterminate", "evidence": consistent,
                      "required_measurement": [
                          "Per-run monotonic Core receipt-to-durable-submit, poll-observation-to-commit, and commit-to-response durations",
                          "Per-run Compute acceptance, claim, executor start/end, and terminal-commit correlation",
                          "At least three same-condition processing repetitions with all observability targets UP",
                      ]}
    conclusion["saturation"] = {
        "repetitions": len(saturation),
        "rejected": [phase["outcomes"]["rejected"] for phase in saturation],
        "excluded_from_work_latency": True,
        "note": "A single rejected burst is not evidence of a Compute or Core bottleneck.",
    }
    conclusion["benchmark_policy"] = {
        "compute_candidate": "Capture a new Mode=Official Compute benchmark in the same environment before optimization; never relabel official=false smoke output.",
        "core_candidate": "An official Compute benchmark cannot prove a Core poll/persistence improvement; preserve before/after E2E load conditions.",
    }
    return conclusion


def missing_observations(phase: dict) -> list[str]:
    missing = []
    for owner, value in phase.get("direct_metrics", {}).items():
        if value.get("state") != "observed":
            missing.append("direct:" + owner)
    for name, value in phase.get("prometheus", {}).get("values", {}).items():
        if value.get("state") != "observed":
            missing.append("prometheus:" + name)
    return missing


def log_match_counts(phases: list[dict]) -> dict:
    ids = {record["run_id"] for phase in phases for record in phase["records"] if record["run_id"]}
    result = {}
    for name in ("core-api", "compute-api", "compute-worker"):
        path = ROOT / ".integration-data/logs" / (name + ".out.log")
        if not path.exists():
            result[name] = {"state": "N/A"}
            continue
        count = 0
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if any(run_id in line for run_id in ids):
                    count += 1
        result[name] = {"state": "checked", "matching_lines": count}
    return result


def markdown(report: dict) -> str:
    lines = ["# Local load test", "", f"실행: {report['started_utc']}",
             f"상태: {report['status']}", f"Fixture: {report.get('fixture', {}).get('snapshot_id', 'N/A')}",
             f"Manifest SHA-256: {report.get('fixture', {}).get('manifest_sha256', 'N/A')}",
             f"환경: {report['environment']}", f"설정: {report['parameters']}",
             f"Core scrape: {report.get('core_scrape', {}).get('state', 'N/A')}",
             f"관측 범위: {report.get('observability_status', 'N/A')}", "",
             "## Revisions", ""]
    for name, value in report["revisions"].items():
        lines.append(f"- {name}: `{value}`")
    lines.extend(["", "## Scenarios", "",
                  "| Scenario | 반복 | 종류 | 제출 | 접수 | 완료 | terminal 실패 | 거절 | timeout | terminal 미확인 | 실제 제출률/s |",
                  "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"])
    for phase in report["phases"]:
        outcomes = phase["outcomes"]
        lines.append(f"| {phase['name']} | {phase['repetition']} | {phase['scenario_kind']} | {phase['submitted']} | {phase['accepted']} | "
                     f"{outcomes['completed']} | {outcomes['failed']} | {outcomes['rejected']} | "
                     f"{outcomes['terminal_timeout']} | {phase['terminal_latency_censored']} | "
                     f"{phase['actual_submit_rate_per_s']} |")
        lines.append(f"\n{phase['name']}: 제출 지연(ms) {phase['submit_latency']}; "
                     f"접수 작업 terminal 지연(ms) {phase['accepted_work_terminal_latency']}; "
                     f"거절 응답 지연(ms) {phase['rejection_response_latency']}. "
                     f"최대 동시 제출 {phase['max_concurrent_submissions']}, poll {phase['poll_requests']}회. "
                     f"HTTP 오류 코드 {phase['http_error_codes']}. "
                     "Terminal 지연은 완료·실패 전이를 모두 포함하고, 미완료 표본 수는 별도 표시합니다.\n")
        lines.append(f"Compute queue 평균(s) {_histogram_mean(phase, 'refinvest_compute_worker_queue_wait_seconds')}; "
                     f"median/p95 {_histogram_quantile(phase, 'refinvest_compute_worker_queue_wait_seconds', 0.5)}/"
                     f"{_histogram_quantile(phase, 'refinvest_compute_worker_queue_wait_seconds', 0.95)}; "
                     f"worker attempt 평균(s) {_histogram_mean(phase, 'refinvest_compute_worker_attempt_duration_seconds')}; "
                     f"median/p95 {_histogram_quantile(phase, 'refinvest_compute_worker_attempt_duration_seconds', 0.5)}/"
                     f"{_histogram_quantile(phase, 'refinvest_compute_worker_attempt_duration_seconds', 0.95)}; "
                     f"관측 누락 {phase.get('observation_missing', [])}.\n")
    if report.get("error"):
        lines.append(f"오류: {report['error']}")
    lines.extend(["", "## Bottleneck assessment", "", str(report.get("bottleneck_assessment", {})),
                  "", f"Clock skew 표시: {report.get('clock_skew', {})}",
                  "", f"DB 대조: {report.get('database', {'state': 'N/A'})}",
                  "", "## Prometheus", ""])
    for name, value in report.get("prometheus", {}).get("values", {}).items():
        lines.append(f"- {name}: {value}")
    lines.extend(["", "## Direct Compute metrics", ""])
    for name, value in report.get("direct_metrics", {}).items():
        lines.append(f"- {name}: {value}")
    lines.extend(["", "N/A는 관측 불가이며 0이 아닙니다.",
                  "부하 결과는 runtime 병목 탐색용이며 benchmark baseline이나 최적화 효과 판정에 쓰지 않습니다.", ""])
    return "\n".join(lines)


def arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-url", default="http://127.0.0.1:8080")
    parser.add_argument("--compute-url", default="http://127.0.0.1:8000")
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--fixture-root", type=Path, default=ROOT / ".integration-data/snapshots/integration-e2e-v1")
    parser.add_argument("--output-root", type=Path, default=ROOT / ".integration-data/performance/load-tests")
    parser.add_argument("--steady-rate", type=float, default=0.2)
    parser.add_argument("--steady-duration", type=float, default=30)
    parser.add_argument("--burst-rate", type=float, default=2)
    parser.add_argument("--burst-duration", type=float, default=10)
    parser.add_argument("--max-submissions", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--terminal-timeout", type=float, default=120)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--cooldown", type=float, default=5)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args(argv)
    if not 0 < args.steady_rate <= 2 or not 0 < args.burst_rate <= 10:
        parser.error("Rates must be positive and at most 2/s steady or 10/s burst")
    if not 1 <= args.max_submissions <= 100 or not 1 <= args.concurrency <= 8:
        parser.error("max-submissions must be 1..100 and concurrency 1..8")
    if not 1 <= args.steady_duration <= 300 or not 1 <= args.burst_duration <= 120:
        parser.error("Durations must be bounded (steady 1..300s, burst 1..120s)")
    if not 5 <= args.terminal_timeout <= 600 or not 0.2 <= args.poll_interval <= 10:
        parser.error("terminal-timeout 5..600s and poll-interval 0.2..10s are required")
    if not 0 <= args.cooldown <= 60:
        parser.error("cooldown must be 0..60s")
    if not 1 <= args.repetitions <= 10:
        parser.error("repetitions must be 1..10")
    for url in (args.core_url, args.compute_url, args.prometheus_url):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
            parser.error("Only local HTTP endpoints are accepted")
    return args


def main(argv: list[str] | None = None) -> int:
    args = arguments(sys.argv[1:] if argv is None else argv)
    token = os.environ.get("REFINVEST_LOAD_ACCESS_TOKEN")
    if not token:
        raise SystemExit("REFINVEST_LOAD_ACCESS_TOKEN is required (use run-load-test.ps1)")
    run_dir = args.output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=False)
    report = {"started_utc": utc_now(), "status": "failed", "phases": [],
              "revisions": {name: revision(path) for name, path in {
                  "integration": ROOT, "context": ROOT.parent / "context", "core": ROOT.parent / "api/refinvest",
                  "compute": ROOT.parent / "compute-api/refinvest", "web": ROOT.parent / "web/web"}.items()},
              "environment": {"platform": platform.platform(), "python": platform.python_version(),
                              "cpu_count": os.cpu_count()},
              "parameters": {name: getattr(args, name) for name in (
                  "steady_rate", "steady_duration", "burst_rate", "burst_duration", "max_submissions",
                  "concurrency", "terminal_timeout", "poll_interval", "cooldown", "repetitions")},
              "stage_model": {
                  "boundaries": ["client submission", "Core receipt/durable enqueue", "Compute acceptance",
                                 "Worker queue wait", "Worker execution", "Compute terminal commit",
                                 "Core poll observation", "Core terminal persistence", "client terminal observation"],
                  "duration_policy": "Only same-process monotonic timers are exact. Wall-clock timestamps are boundary evidence, not cross-service durations.",
                  "measurement_semantics": {
                      "client_submit_and_terminal": "load-generator time.monotonic durations",
                      "core_compute_client": "Micrometer Timer around the Core HTTP client",
                      "core_scheduler_lag": "same-process wall-clock Duration; displayed but not treated as monotonic exact duration",
                      "compute_queue_wait": "API-created DB timestamp to Worker wall clock; displayed with clock-skew caveat",
                      "worker_attempt": "Worker monotonic duration including executor/IPC and heartbeat shutdown",
                      "compute_terminal": "counter increments only after committed terminal persistence",
                      "core_terminal": "counter/log emitted only after terminal transaction commit; DB state is re-read",
                      "unavailable_exact_segments": "per-run Core poll observation-to-terminal commit and terminal commit-to-client response",
                  },
              }}
    first_measurement = None
    direct_start = None
    try:
        report["fixture"] = fixture_identity(args.fixture_root)
        for url in (args.core_url + "/actuator/health", args.compute_url + "/health"):
            status, _, _ = http_json(url, timeout=3)
            if status != 200:
                raise RuntimeError(f"Health preflight failed: HTTP {status}")
        report["core_scrape"] = probe_core_scrape(args.core_url)
        report["clock_skew"] = {"core": clock_skew_probe(args.core_url + "/actuator/health"),
                                "compute": clock_skew_probe(args.compute_url + "/health")}
        version_id = create_version(args.core_url, token)
        warmup = submit(args.core_url, version_id, token, 0, time.monotonic())
        report["warmup"] = {key: value for key, value in warmup.items() if key != "run_id"}
        if not warmup["accepted"]:
            raise RuntimeError(f"Warm-up submission failed: HTTP {warmup['http_status']}")
        poll_one(args.core_url, token, warmup, args.terminal_timeout, args.poll_interval)
        report["warmup"]["outcome"] = warmup["outcome"]
        if warmup["outcome"] != "completed":
            raise RuntimeError(f"Warm-up did not complete: {warmup['outcome']}")
        report["observability_preflight"] = prometheus_preflight(args.prometheus_url)
        if report["observability_preflight"]["state"] != "ready":
            raise RuntimeError("Prometheus targets or required metric series are not ready")
        direct_start = direct_snapshot()
        first_measurement = time.time()
        for scenario_kind, rate, duration in (("processing", args.steady_rate, args.steady_duration),
                                               ("saturation", args.burst_rate, args.burst_duration)):
            for repetition in range(1, args.repetitions + 1):
                if report["phases"]:
                    time.sleep(args.cooldown)
                metric_start = direct_snapshot()
                phase_wall_start = time.time()
                phase = run_phase(scenario_kind, args.core_url, version_id, token, rate, duration,
                                  args.max_submissions, args.concurrency, args.terminal_timeout,
                                  args.poll_interval, repetition, scenario_kind)
                phase_wall_end = time.time()
                phase["direct_metrics"] = direct_delta(metric_start, direct_snapshot())
                phase["prometheus"] = prometheus_summary(args.prometheus_url, phase_wall_start, phase_wall_end)
                phase["observation_missing"] = missing_observations(phase)
                report["phases"].append(phase)
        report["database"] = verify_database(report["phases"])
        report["log_matches"] = log_match_counts(report["phases"])
        report["cross_clock_stage_summary"] = cross_clock_stage_summary(
            report["phases"], report["database"], report["clock_skew"]
        )
        report["bottleneck_assessment"] = analyze_repetitions(report["phases"])
        processing_ok = all(phase["outcomes"]["completed"] == phase["submitted"]
                            for phase in report["phases"] if phase["scenario_kind"] == "processing")
        saturation_ok = all(phase["outcomes"]["completed"] + phase["outcomes"]["rejected"] == phase["submitted"]
                            for phase in report["phases"] if phase["scenario_kind"] == "saturation")
        report["saturation_reached"] = any(phase["outcomes"]["rejected"] > 0
                                           for phase in report["phases"] if phase["scenario_kind"] == "saturation")
        report["status"] = "passed" if processing_ok and saturation_ok and report["saturation_reached"] \
            and report["database"]["state"] == "verified" else "failed"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        end_measurement = time.time()
        report["ended_utc"] = utc_now()
        if direct_start is not None:
            report["direct_metrics"] = direct_delta(direct_start, direct_snapshot())
        if first_measurement is not None:
            report["prometheus"] = prometheus_summary(args.prometheus_url, first_measurement, end_measurement)
        targets = report.get("prometheus", {}).get("values", {}).get("targets", {})
        up_jobs = {item["labels"].get("job") for item in targets.get("series", []) if item.get("value") == "1"}
        report["observability_status"] = "complete" if (
            report.get("observability_preflight", {}).get("state") == "ready"
            and {"core-api", "compute-api", "compute-worker"}.issubset(up_jobs)
        ) else "partial"
        (run_dir / "result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (run_dir / "summary.md").write_text(markdown(report), encoding="utf-8")
        print(run_dir)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
