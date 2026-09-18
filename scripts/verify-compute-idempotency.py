"""Measure Compute HTTP idempotency against an isolated PostgreSQL schema."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import partial
from pathlib import Path
from threading import Barrier
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from psycopg import connect


def request_payload(round_number: int) -> dict[str, object]:
    return {
        "strategyVersionId": f"reliability-http-{round_number}-{uuid4()}",
        "strategyVersion": {
            "primarySignalAsset": "QQQ",
            "conditions": [
                {
                    "operator": "GT",
                    "logicalCombinator": None,
                    "operandA": {"asset": "QQQ", "metric": "SIMPLE", "window": None},
                    "operandB": "1000",
                }
            ],
            "executionAsset": "QQQ",
            "lag": 0,
            "exit": {"holdingSignalSessions": 1},
            "positionPolicy": {
                "longOnly": True,
                "singlePosition": True,
                "duplicateEntry": "IGNORE",
            },
        },
        "feeModel": {"commission": "0", "slippage": "0"},
        "period": {"start": "2025-01-02", "end": "2025-01-08"},
        "datasetSnapshotId": "integration-e2e-v1",
    }


def submit(
    base_url: str, api_key: str, key: str, body: bytes, timeout: float
) -> tuple[int | None, dict[str, object] | None, str | None]:
    request = Request(
        f"{base_url.rstrip('/')}/backtests",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Api-Key": api_key,
            "Idempotency-Key": key,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read()), None
    except HTTPError as error:
        try:
            payload = json.loads(error.read())
        except (ValueError, UnicodeDecodeError):
            payload = None
        return error.code, payload, None
    except (TimeoutError, URLError, ConnectionError) as error:
        return None, None, type(error).__name__


def synchronized_submit(
    _index: int,
    *,
    barrier: Barrier,
    base_url: str,
    api_key: str,
    key: str,
    body: bytes,
    timeout: float,
) -> tuple[int | None, dict[str, object] | None, str | None]:
    barrier.wait(timeout=timeout)
    return submit(base_url, api_key, key, body, timeout)


def assert_database(database_url: str) -> None:
    database = urlsplit(database_url).path.lstrip("/")
    if database != "refinvest_integration":
        raise RuntimeError("Reliability checks require the refinvest_integration database.")
    with connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        if cursor.fetchone()[0] != "refinvest_integration":
            raise RuntimeError("Connected PostgreSQL database is not refinvest_integration.")


def read_job(database_url: str, key_hash: str) -> list[tuple[object, ...]]:
    with connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT run_id::text, strategy_version_id, request_payload,
                   request_fingerprint, status
            FROM compute_backtest_jobs
            WHERE idempotency_key_hash = %s
            ORDER BY run_id
            """,
            (key_hash,),
        )
        return list(cursor.fetchall())


def delete_owned_job(database_url: str, key_hash: str) -> None:
    with connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM compute_backtest_jobs WHERE idempotency_key_hash = %s",
            (key_hash,),
        )


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.concurrency <= 0 or args.rounds <= 0:
        raise SystemExit("concurrency and rounds must be positive")

    database_url = os.environ.get("REFINVEST_DATABASE_URL")
    api_key = os.environ.get("REFINVEST_INTERNAL_API_KEY")
    if not database_url or not api_key:
        raise SystemExit("REFINVEST_DATABASE_URL and REFINVEST_INTERNAL_API_KEY are required.")
    assert_database(database_url)

    successful_202 = 0
    unexpected_5xx = 0
    connection_errors = 0
    unique_jobs = 0
    duplicate_jobs = 0
    conflict_attempts = 0
    responses_409 = 0
    unexpected_conflicts = 0
    mutated_jobs = 0
    additional_jobs = 0
    failures: list[str] = []

    for round_number in range(1, args.rounds + 1):
        key = str(uuid4())
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        payload = request_payload(round_number)
        body = json.dumps(payload, separators=(",", ":")).encode()
        barrier = Barrier(args.concurrency)

        try:
            round_submit = partial(
                synchronized_submit,
                barrier=barrier,
                base_url=args.base_url,
                api_key=api_key,
                key=key,
                body=body,
                timeout=args.timeout,
            )
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                responses = list(executor.map(round_submit, range(args.concurrency)))
            statuses = [status for status, _, _ in responses]
            successful_202 += statuses.count(202)
            unexpected_5xx += sum(status is not None and status >= 500 for status in statuses)
            connection_errors += sum(error is not None for _, _, error in responses)
            response_run_ids = {
                str(response["runId"])
                for status, response, _ in responses
                if status == 202 and response is not None and "runId" in response
            }
            rows_before = read_job(database_url, key_hash)
            unique_jobs += len(rows_before)
            duplicate_jobs += max(0, len(rows_before) - 1)

            if statuses.count(202) != args.concurrency:
                failures.append(f"round {round_number}: not all responses were 202")
            if len(response_run_ids) != 1:
                failures.append(f"round {round_number}: response run IDs did not converge")
            if len(rows_before) != 1:
                failures.append(f"round {round_number}: expected exactly one database job")
                continue
            original = rows_before[0]
            if response_run_ids != {str(original[0])}:
                failures.append(f"round {round_number}: HTTP and database run IDs differ")

            conflict = deepcopy(payload)
            conflict["feeModel"] = {"commission": "0.01", "slippage": "0"}
            conflict_attempts += 1
            conflict_status, _, conflict_error = submit(
                args.base_url,
                api_key,
                key,
                json.dumps(conflict, separators=(",", ":")).encode(),
                args.timeout,
            )
            if conflict_status == 409:
                responses_409 += 1
            else:
                unexpected_conflicts += 1
                failures.append(
                    f"round {round_number}: conflict returned {conflict_status or conflict_error}"
                )
            rows_after = read_job(database_url, key_hash)
            additional_jobs += max(0, len(rows_after) - len(rows_before))
            if rows_after != rows_before:
                mutated_jobs += 1
                failures.append(f"round {round_number}: conflict mutated the existing job")
        except Exception as error:  # noqa: BLE001 - preserve partial evidence and clean owned row.
            failures.append(f"round {round_number}: {type(error).__name__}: {error}")
        finally:
            delete_owned_job(database_url, key_hash)

    result: dict[str, object] = {
        "idempotencyConcurrency": {
            "concurrency": args.concurrency,
            "rounds": args.rounds,
            "totalRequests": args.concurrency * args.rounds,
            "successful202": successful_202,
            "logicalRequests": args.rounds,
            "uniqueJobs": unique_jobs,
            "duplicateJobs": duplicate_jobs,
            "unexpected5xx": unexpected_5xx,
            "connectionErrors": connection_errors,
        },
        "payloadConflicts": {
            "attempts": conflict_attempts,
            "responses409": responses_409,
            "unexpectedResponses": unexpected_conflicts,
            "mutatedExistingJobs": mutated_jobs,
            "additionalJobs": additional_jobs,
        },
        "failures": failures,
    }
    write_json(args.output, result)
    print(json.dumps(result, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
