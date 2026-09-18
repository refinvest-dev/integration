"""Drive Core public HTTP and verify durable dispatch state in PostgreSQL."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from psycopg import connect, sql


def validate_schema(value: str) -> str:
    if not value or not value[0].islower() or not value.replace("_", "").isalnum():
        raise ValueError("Schema must be a lowercase PostgreSQL identifier.")
    if value != value.lower() or not value[0].isalpha():
        raise ValueError("Schema must be a lowercase PostgreSQL identifier.")
    return value


def database_url() -> str:
    value = os.environ.get("REFINVEST_DATABASE_URL")
    if not value:
        raise RuntimeError("REFINVEST_DATABASE_URL is required.")
    parsed = urlsplit(value)
    if parsed.path.lstrip("/") != "refinvest_integration":
        raise RuntimeError("Reliability checks require the refinvest_integration database.")
    return value


def jwt(member_id: int) -> str:
    def part(value: object) -> bytes:
        encoded = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(encoded).rstrip(b"=")

    now = int(time.time())
    header = part({"alg": "HS256", "typ": "JWT"})
    claims = part(
        {
            "sub": str(member_id),
            "role": "MEMBER",
            "iss": os.environ["REFINVEST_JWT_ISSUER"],
            "aud": [os.environ["REFINVEST_JWT_AUDIENCE"]],
            "iat": now,
            "exp": now + 3600,
            "jti": str(uuid4()),
            "typ": "access",
        }
    )
    signed = header + b"." + claims
    signature = base64.urlsafe_b64encode(
        hmac.new(os.environ["REFINVEST_JWT_SECRET"].encode(), signed, hashlib.sha256).digest()
    ).rstrip(b"=")
    return (signed + b"." + signature).decode()


def http_json(
    method: str,
    url: str,
    token: str | None = None,
    body: dict[str, object] | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, object]]:
    csrf = "integration-reliability-csrf"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Cookie"] = f"REFINVEST_ACCESS_TOKEN={token}; XSRF-TOKEN={csrf}"
        headers["X-XSRF-TOKEN"] = csrf
    request = Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
            return response.status, payload
    except HTTPError as error:
        try:
            payload = json.loads(error.read())
        except (ValueError, UnicodeDecodeError):
            payload = {"message": "non-JSON error response"}
        return error.code, payload


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def seed_member(connection: object, schema: str, member_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "INSERT INTO {}.members (id, role, created_at) "
                "VALUES (%s, 'MEMBER', CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING"
            ).format(sql.Identifier(schema)),
            (member_id,),
        )
        cursor.execute(
            sql.SQL(
                "INSERT INTO {}.subscriptions (member_id, tier) VALUES (%s, 'PRO') "
                "ON CONFLICT (member_id) DO UPDATE SET tier = EXCLUDED.tier"
            ).format(sql.Identifier(schema)),
            (member_id,),
        )


def submit_mode(args: argparse.Namespace) -> int:
    core_schema = validate_schema(args.core_schema)
    compute_schema = validate_schema(args.compute_schema)
    db_url = database_url()
    campaign = uuid4().hex
    member_base = 8_000_000_000_000_000_000 + int(campaign[:10], 16)
    entries: list[dict[str, object]] = []
    failures: list[str] = []

    with connect(db_url) as connection:
        for index in range(args.count):
            member_id = member_base + index
            seed_member(connection, core_schema, member_id)
            token = jwt(member_id)
            status, strategy = http_json(
                "POST",
                f"{args.core_base_url}/strategies",
                token,
                {"name": f"Reliability {campaign[:8]} #{index + 1}"},
            )
            if status not in {200, 201} or "id" not in strategy:
                failures.append(f"strategy creation {index + 1} returned HTTP {status}")
                continue
            strategy_id = str(strategy["id"])
            status, version = http_json(
                "POST",
                f"{args.core_base_url}/strategies/{strategy_id}/versions",
                token,
                {
                    "primarySignalAsset": "QQQ",
                    "executionAsset": "QQQ",
                    "lag": 0,
                    "conditions": [
                        {
                            "operator": "GT",
                            "logicalCombinator": None,
                            "operandA": {
                                "asset": "QQQ",
                                "metric": "SIMPLE",
                                "window": None,
                            },
                            "operandB": 1000,
                        }
                    ],
                    "exit": {"holdingSignalSessions": 1},
                },
            )
            if status not in {200, 201} or "id" not in version:
                failures.append(f"version creation {index + 1} returned HTTP {status}")
                continue
            version_id = str(version["id"])
            status, run = http_json(
                "POST",
                f"{args.core_base_url}/strategy-versions/{version_id}/backtests",
                token,
                {
                    "period": {"start": "2025-01-02", "end": "2025-01-08"},
                    "feeModel": {"commission": 0, "slippage": 0},
                },
            )
            if status != 202 or "id" not in run:
                failures.append(f"backtest submission {index + 1} returned HTTP {status}")
                continue
            entries.append(
                {
                    "memberId": member_id,
                    "strategyId": strategy_id,
                    "strategyVersionId": version_id,
                    "backtestRunId": str(run["id"]),
                }
            )

    run_ids = [int(str(entry["backtestRunId"])) for entry in entries]
    persisted_runs = 0
    pending_runs = 0
    persisted_dispatches = 0
    compute_jobs = 0
    if run_ids:
        with connect(db_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT status, count(*) FROM {}.backtest_runs "
                    "WHERE id = ANY(%s) GROUP BY status"
                ).format(sql.Identifier(core_schema)),
                (run_ids,),
            )
            status_counts = dict(cursor.fetchall())
            persisted_runs = sum(status_counts.values())
            pending_runs = status_counts.get("PENDING", 0)
            cursor.execute(
                sql.SQL(
                    "SELECT count(*) FROM {}.backtest_compute_dispatches "
                    "WHERE backtest_run_id = ANY(%s)"
                ).format(sql.Identifier(core_schema)),
                (run_ids,),
            )
            persisted_dispatches = cursor.fetchone()[0]
            version_ids = [str(entry["strategyVersionId"]) for entry in entries]
            cursor.execute(
                sql.SQL(
                    "SELECT count(*) FROM {}.compute_backtest_jobs "
                    "WHERE request_payload->>'strategyVersionId' = ANY(%s)"
                ).format(sql.Identifier(compute_schema)),
                (version_ids,),
            )
            compute_jobs = cursor.fetchone()[0]

    if len(entries) != args.count:
        failures.append("not all requested Core runs were submitted")
    if persisted_runs != len(entries) or pending_runs != len(entries):
        failures.append("Core did not retain every submitted run as PENDING")
    if persisted_dispatches != len(entries):
        failures.append("Core did not persist one dispatch per submitted run")
    if compute_jobs != 0:
        failures.append("Compute jobs existed before Compute recovery")

    state: dict[str, object] = {
        "campaign": campaign,
        "createdAt": datetime.now(UTC).isoformat(),
        "coreSchema": core_schema,
        "computeSchema": compute_schema,
        "entries": entries,
    }
    write_json(args.state, state)
    result: dict[str, object] = {
        "submittedRuns": len(entries),
        "persistedRunsBeforeComputeRecovery": persisted_runs,
        "pendingRunsBeforeComputeRecovery": pending_runs,
        "persistedDispatchesBeforeComputeRecovery": persisted_dispatches,
        "computeJobsBeforeRecovery": compute_jobs,
        "failures": failures,
    }
    write_json(args.output, result)
    print(json.dumps(result, indent=2))
    return 0 if not failures else 1


def recover_mode(args: argparse.Namespace) -> int:
    state = json.loads(args.state.read_text(encoding="utf-8"))
    core_schema = validate_schema(str(state["coreSchema"]))
    compute_schema = validate_schema(str(state["computeSchema"]))
    entries = state["entries"]
    if not isinstance(entries, list):
        raise TypeError("Invalid dispatch state file.")
    db_url = database_url()
    started_at = datetime.fromisoformat(args.recovery_started_at).astimezone(UTC)
    deadline = time.monotonic() + args.timeout

    def poll(entry: dict[str, object]) -> tuple[str, dict[str, object] | None, str | None]:
        run_id = str(entry["backtestRunId"])
        token = jwt(int(entry["memberId"]))
        last_transient_error: str | None = None
        while time.monotonic() < deadline:
            try:
                status, payload = http_json(
                    "GET", f"{args.core_base_url}/backtest-runs/{run_id}", token
                )
                if status != 200:
                    last_transient_error = f"poll returned HTTP {status}"
                    time.sleep(1)
                    continue
                if payload.get("status") not in {"PENDING", "RUNNING"}:
                    return run_id, payload, None
            except (OSError, URLError, TimeoutError) as error:
                last_transient_error = f"poll failed with {type(error).__name__}"
            time.sleep(1)
        detail = f"; last transient error: {last_transient_error}" if last_transient_error else ""
        return run_id, None, f"terminal status timeout{detail}"

    with ThreadPoolExecutor(max_workers=min(len(entries), 20)) as executor:
        polled = list(executor.map(poll, entries))
    poll_failures = [error for _, _, error in polled if error is not None]

    run_ids = [int(str(entry["backtestRunId"])) for entry in entries]
    version_ids = [str(entry["strategyVersionId"]) for entry in entries]
    with connect(db_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "SELECT id, status FROM {}.backtest_runs WHERE id = ANY(%s)"
            ).format(sql.Identifier(core_schema)),
            (run_ids,),
        )
        core_runs = {str(run_id): status for run_id, status in cursor.fetchall()}
        cursor.execute(
            sql.SQL(
                "SELECT backtest_run_id, status, compute_run_id, next_attempt_at, "
                "lease_token FROM {}.backtest_compute_dispatches "
                "WHERE backtest_run_id = ANY(%s)"
            ).format(sql.Identifier(core_schema)),
            (run_ids,),
        )
        dispatches = {str(row[0]): row[1:] for row in cursor.fetchall()}
        cursor.execute(
            sql.SQL(
                "SELECT run_id::text, request_payload->>'strategyVersionId', status "
                "FROM {}.compute_backtest_jobs "
                "WHERE request_payload->>'strategyVersionId' = ANY(%s)"
            ).format(sql.Identifier(compute_schema)),
            (version_ids,),
        )
        compute_rows = list(cursor.fetchall())

    observed_at = datetime.now(UTC)
    completed = sum(status == "COMPLETED" for status in core_runs.values())
    failed = sum(status == "FAILED" for status in core_runs.values())

    missing_dispatch_pending = sum(
        status == "PENDING" and run_id not in dispatches for run_id, status in core_runs.items()
    )
    now = datetime.now(UTC)
    pending_dispatches = [row for row in dispatches.values() if row[0] == "PENDING"]
    pending_retry_future = sum(
        row[2] is not None and row[2].astimezone(UTC) > now for row in pending_dispatches
    )
    pending_retry_ready = sum(
        row[2] is not None and row[2].astimezone(UTC) <= now and row[3] is None
        for row in pending_dispatches
    )
    pending_leased = sum(row[3] is not None for row in pending_dispatches)

    counts_by_version: dict[str, int] = {}
    for _, version_id, _ in compute_rows:
        counts_by_version[str(version_id)] = counts_by_version.get(str(version_id), 0) + 1
    duplicate_compute_jobs = sum(max(0, count - 1) for count in counts_by_version.values())
    compute_ids = {row[0] for row in compute_rows}
    mapping_mismatches = sum(
        row[1] is None or str(row[1]) not in compute_ids for row in dispatches.values()
    )

    proxy_status_code, proxy_status = http_json("GET", args.proxy_status_url)
    dropped_run_id = proxy_status.get("droppedComputeRunId")
    dropped_job_count = sum(row[0] == dropped_run_id for row in compute_rows)
    response_loss_executed = (
        proxy_status_code == 200
        and proxy_status.get("droppedAcceptedResponses") == 1
        and proxy_status.get("sameKeyRetryObserved") is True
    )

    failures: list[str] = list(poll_failures)
    if completed != len(entries):
        failures.append("not all submitted runs recovered to COMPLETED")
    if failed:
        failures.append(f"{failed} Core runs reached unexpected FAILED status")
    if missing_dispatch_pending:
        failures.append("a PENDING Core run has no dispatch row")
    if duplicate_compute_jobs:
        failures.append("duplicate Compute jobs were created")
    if len(compute_rows) != len(entries):
        failures.append("Compute job count does not match submitted Core runs")
    if mapping_mismatches:
        failures.append("Core dispatch to Compute run mapping mismatch")
    if not response_loss_executed:
        failures.append("response-loss retry was not fully observed")
    if dropped_job_count != 1:
        failures.append("dropped-response logical request did not map to one Compute job")

    result: dict[str, object] = {
        "dispatchRecovery": {
            "submittedRuns": len(entries),
            "persistedDispatches": args.persisted_dispatches,
            "recoveredTerminalRuns": completed + failed,
            "recoveredCompletedRuns": completed,
            "recoveredFailedRuns": failed,
            "orphanPendingRuns": missing_dispatch_pending,
            "pendingDispatchRetryNotDue": pending_retry_future,
            "pendingDispatchRetryReady": pending_retry_ready,
            "pendingDispatchLeased": pending_leased,
            "uniqueComputeJobs": len(compute_ids),
            "duplicateComputeJobs": duplicate_compute_jobs,
            "mappingMismatches": mapping_mismatches,
            "recoveryDurationSeconds": round((observed_at - started_at).total_seconds(), 3),
            "unexpectedFailures": len(failures),
        },
        "responseLoss": {
            "executed": response_loss_executed,
            "logicalRequests": 1 if proxy_status.get("droppedAcceptedResponses") == 1 else 0,
            "retryRequests": int(proxy_status.get("retryRequests", 0)),
            "uniqueComputeJobs": dropped_job_count,
            "duplicateComputeJobs": max(0, dropped_job_count - 1),
            "forwardedSubmissionRequests": int(
                proxy_status.get("forwardedSubmissionRequests", 0)
            ),
        },
        "failures": failures,
    }
    write_json(args.output, result)
    print(json.dumps(result, indent=2))
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("--core-base-url", default="http://127.0.0.1:8080")
    submit_parser.add_argument("--core-schema", required=True)
    submit_parser.add_argument("--compute-schema", required=True)
    submit_parser.add_argument("--count", type=int, default=10)
    submit_parser.add_argument("--state", type=Path, required=True)
    submit_parser.add_argument("--output", type=Path, required=True)
    submit_parser.set_defaults(handler=submit_mode)

    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--core-base-url", default="http://127.0.0.1:8080")
    recover_parser.add_argument(
        "--proxy-status-url", default="http://127.0.0.1:8001/__faults__/status"
    )
    recover_parser.add_argument("--state", type=Path, required=True)
    recover_parser.add_argument("--output", type=Path, required=True)
    recover_parser.add_argument("--timeout", type=float, default=180.0)
    recover_parser.add_argument("--recovery-started-at", required=True)
    recover_parser.add_argument("--persisted-dispatches", type=int, required=True)
    recover_parser.set_defaults(handler=recover_mode)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
