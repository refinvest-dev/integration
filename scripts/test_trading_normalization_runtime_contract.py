"""HTTP/DB assertions for the actual Core -> Compute normalization boundary.

Only the negative cases submit directly to Compute. The primary request must
originate at Core's public upload/validate API and durable dispatcher.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import jsonschema
import psycopg
import yaml
from trading_normalization_result_verifier import (
    SPEC,
    IntegrityFailure,
    collect_v1_resource,
    negative_scenarios,
    pagination_independence,
    schema_validate,
    self_test,
    verify,
    verify_transport,
)


class ScenarioFailure(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ScenarioFailure(code)


def mark_pass(evidence: dict, scenario: str, started: float) -> None:
    evidence["scenarios"][scenario] = "PASS"
    evidence.setdefault("scenarioDurationsSeconds", {})[scenario] = round(
        time.monotonic() - started, 3)


def git_revision(path: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()


def request(client: httpx.Client, method: str, url: str, expected: int, **kwargs: object) -> dict:
    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as error:
        raise ScenarioFailure("HTTP_TRANSPORT_FAILURE", type(error).__name__) from None
    if response.status_code != expected:
        # Never include response text: it may contain a signed locator or raw input.
        code = None
        try:
            code = response.json().get("code")
        except (ValueError, AttributeError):
            pass
        raise ScenarioFailure(str(code or "HTTP_STATUS_MISMATCH"), f"{method} status={response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise ScenarioFailure("RESPONSE_JSON_INVALID") from None


def db_connection(schema: str) -> psycopg.Connection:
    name = os.environ["TRADING_RUNTIME_DB_NAME"]
    require(name == "refinvest_integration", "UNSAFE_DATABASE_NAME")
    require(schema == "public" or
            (schema.startswith("tnorm_") and schema[6:].isalnum()), "UNSAFE_DATABASE_SCHEMA")
    connection = psycopg.connect(
        host=os.environ["TRADING_RUNTIME_DB_HOST"],
        port=int(os.environ["TRADING_RUNTIME_DB_PORT"]),
        dbname=name,
        user=os.environ["TRADING_RUNTIME_DB_USER"],
        password=os.environ["TRADING_RUNTIME_DB_PASSWORD"],
        options=f"-c search_path={schema}",
        autocommit=True,
    )
    with connection.cursor() as cursor:
        cursor.execute("select current_database(), current_schema()")
        require(cursor.fetchone() == (name, schema), "UNSAFE_DATABASE_TARGET")
    return connection


def row(connection: psycopg.Connection, query: str, args: tuple) -> dict | None:
    with connection.cursor(row_factory=psycopg.rows.dict_row) as cursor:
        cursor.execute(query, args)
        value = cursor.fetchone()
        return dict(value) if value else None


def poll(fetch, accept, timeout: int, label: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fetch()
        if last is not None and accept(last):
            return last
        time.sleep(0.5)
    raise ScenarioFailure("TIMEOUT", f"{label} last={safe_state(last)}")


def safe_state(value) -> str:
    if isinstance(value, dict):
        return str(value.get("status") or value.get("code") or "observed")
    return "missing" if value is None else "observed"


def upload(client: httpx.Client, core_url: str, session_id: str, role: str,
           path: Path, expected_sha: str) -> dict:
    content = path.read_bytes()
    require(hashlib.sha256(content).hexdigest() == expected_sha, "FIXTURE_SHA256_MISMATCH")
    metadata = {
        "role": role, "filename": path.name, "contentType": "text/csv",
        "byteSize": len(content), "sha256": expected_sha,
        "dialectCandidate": "BINANCE_USDS_TRADE_HISTORY_V1" if role == "TRADE_HISTORY"
        else "BINANCE_USDS_POSITION_HISTORY_V1",
    }
    return request(
        client, "POST", f"{core_url}/trading-import-sessions/{session_id}/artifacts", 201,
        headers={"Idempotency-Key": f"upload-{uuid.uuid4().hex}"},
        files={"file": (path.name, content, "text/csv"),
               "metadata": (None, json.dumps(metadata), "application/json")},
    )


def missing_maker_column(source: Path, destination: Path) -> str:
    """Make a non-sensitive source rejection fixture without changing trade values."""
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        require(reader.fieldnames is not None and "Maker" in reader.fieldnames,
                "SOURCE_FIXTURE_HEADER_INVALID")
        headers = [name for name in reader.fieldnames if name != "Maker"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=headers, lineterminator="\r\n",
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(reader)
    content = output.getvalue().encode("utf-8-sig")
    destination.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def check_schema(context: Path, payload: dict) -> None:
    document = yaml.safe_load((context / "openapi" / "compute-api.yaml").read_text(encoding="utf-8"))
    schema = document["components"]["schemas"]["NormalizationTerminalPayload"]
    resolver = jsonschema.RefResolver.from_schema(document)
    jsonschema.validate(payload, schema, resolver=resolver)


def negative_request(stored: dict, core_url: str) -> dict:
    """Restore DTO shape after Compute has removed grant locators from persistence."""
    value = copy.deepcopy(stored)
    for grant in value["artifactAccessGrants"]:
        grant["accessUri"] = f"{core_url}/__integration_inert_grant"
    return value


def source_fault_server(port: int, bodies: dict[str, bytes]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = bodies.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def process_script(path: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(path), *arguments],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    require(result.returncode == 0, "PROCESS_CONTROL_FAILED")


def health_code(client: httpx.Client, url: str) -> int:
    try:
        return client.get(url).status_code
    except httpx.HTTPError:
        return 0


def assert_no_result(client: httpx.Client, compute_url: str, job_id: str) -> None:
    try:
        response = client.get(
            f"{compute_url}/trading-review/normalization-jobs/{job_id}/result")
    except httpx.HTTPError:
        raise ScenarioFailure("HTTP_TRANSPORT_FAILURE") from None
    require(response.status_code != 200, "UNEXPECTED_SUCCESS_RESULT")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-url", required=True)
    parser.add_argument("--compute-url", required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--context-path", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--fixture-port", type=int, required=True)
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--core-path", type=Path, required=True)
    parser.add_argument("--compute-path", type=Path, required=True)
    parser.add_argument("--compute-schema", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--result-integrity", action="store_true")
    args = parser.parse_args()
    if args.result_integrity:
        self_test(args.context_path)
    evidence: dict[str, object] = {
        "runId": args.run_dir.name,
        "contextCommit": git_revision(args.context_path),
        "coreCommit": git_revision(args.core_path),
        "computeCommit": git_revision(args.compute_path),
        "contractSync": "PASS", "fixedVector": "PASS", "scenarios": {},
        "computeSchema": os.environ["TRADING_RUNTIME_COMPUTE_SCHEMA"],
    }
    if args.result_integrity:
        evidence["resultFixedVector"] = "PASS"
        evidence["runtimeIdentityGate"] = os.environ.get("TRADING_RUNTIME_IDENTITY_GATE", "NOT_RUN")
        evidence["resultHashSpecificationVersion"] = SPEC
    vector_paths = {
        "context": args.context_path / "fixtures/trading-normalization/input-identity-v1.json",
        "core": args.core_path / "tradingreview/application/src/test/resources/trading-normalization/input-identity-v1.json",
        "compute": args.compute_path / "tests/fixtures/trading_review/normalization_identity/v1/input-identity-v1.json",
    }
    evidence["fixedVectorSha256"] = json.loads(
        vector_paths["context"].read_text(encoding="utf-8"))["expectedSha256"]
    evidence["fixedVectorFileSha256"] = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in vector_paths.items()
    }
    require(len(set(evidence["fixedVectorFileSha256"].values())) == 1,
            "FIXED_VECTOR_SNAPSHOT_DRIFT")
    evidence["logPaths"] = {
        role: str(args.test_root / ".integration-data" / "logs" /
                  f"{args.registry}-{role}.out.log")
        for role in ("core-api", "compute-api", "compute-worker")
    }
    evidence_path = args.run_dir / "evidence.json"
    scenario = "primary"
    scenario_started = time.monotonic()
    started = time.monotonic()
    connection = None
    compute_db = None
    fault_server = None
    try:
        manifest = json.loads((args.fixture_root / "fixture-manifest.json").read_text(encoding="utf-8"))
        artifacts = {a["role"]: a for a in manifest["artifacts"]}
        connection = db_connection("public")
        compute_db = db_connection(os.environ["TRADING_RUNTIME_COMPUTE_SCHEMA"])
        token = os.environ["TRADING_RUNTIME_ACCESS_TOKEN"]
        key = os.environ["TRADING_RUNTIME_API_KEY"]
        csrf = "trading-runtime-csrf"
        with httpx.Client(timeout=15, headers={
            "Cookie": f"REFINVEST_ACCESS_TOKEN={token}; XSRF-TOKEN={csrf}",
            "X-XSRF-TOKEN": csrf,
        }) as core, httpx.Client(timeout=15, headers={"X-Internal-Api-Key": key}) as compute:
            account = request(core, "POST", f"{args.core_url}/trading-accounts", 201,
                              headers={"Idempotency-Key": f"account-{uuid.uuid4().hex}"},
                              json={"venue": "BINANCE", "displayName": "Runtime synthetic"})
            book = request(core, "POST", f"{args.core_url}/trading-accounts/{account['id']}/books", 201,
                           headers={"Idempotency-Key": f"book-{uuid.uuid4().hex}"},
                           json={"displayName": "Runtime synthetic", "productFamily": "LINEAR_PERPETUAL",
                                 "positionMode": "ONE_WAY", "settlementAsset": "USDT", "reviewTimezone": "UTC"})
            session = request(core, "POST", f"{args.core_url}/trading-books/{book['id']}/imports", 201,
                              headers={"Idempotency-Key": f"import-{uuid.uuid4().hex}"},
                              json={"sourceTimezone": "Asia/Seoul", "reviewTimezone": "UTC",
                                    "declaredPositionMode": "ONE_WAY"})
            session_id = session["id"]
            evidence["coreSessionId"] = session_id
            uploaded = {}
            for role in ("TRADE_HISTORY", "POSITION_HISTORY"):
                item = artifacts[role]
                uploaded[role] = upload(core, args.core_url, session_id, role,
                                        args.fixture_root / item["path"], item["sha256"])
                require(uploaded[role]["sha256"] == item["sha256"], "UPLOAD_HASH_MISMATCH")
            current = request(core, "GET", f"{args.core_url}/trading-import-sessions/{session_id}", 200)
            require(current["canValidate"] and set(current["uploadedArtifactRoles"]) == set(artifacts),
                    "UPLOAD_INCOMPLETE")
            validated = request(core, "POST", f"{args.core_url}/trading-import-sessions/{session_id}/validation",
                                202, headers={"Idempotency-Key": f"validate-{uuid.uuid4().hex}"}, json={})
            require(validated["status"] == "VALIDATING", "SESSION_NOT_VALIDATING")
            run = poll(lambda: row(connection, """select id::text, input_fingerprint, logical_key,
                input_identity_json, target_version_set_json, retry_policy_version,
                deletion_generation, compute_job_id, status, last_failure_code from trading_normalization_runs
                where import_session_id=%s order by created_at desc limit 1""", (int(session_id),)),
                       lambda r: r["compute_job_id"] is not None or r["status"] == "TERMINAL_FAILURE",
                       args.timeout, "Core dispatch")
            require(run["compute_job_id"] is not None,
                    str(run["last_failure_code"] or "CORE_DISPATCH_FAILED"))
            run_id, job_id = run["id"], run["compute_job_id"]
            evidence["coreRunId"], evidence["computeJobId"] = run_id, job_id
            dispatch = row(connection, """select id::text, compute_key, status from
                trading_normalization_dispatches where run_id=%s::uuid order by created_at limit 1""", (run_id,))
            require(dispatch is not None and dispatch["status"] in {"SUBMITTED", "RESULT_READY"},
                    "DISPATCH_NOT_SUBMITTED")
            job = row(compute_db, """select job_id::text, core_normalization_run_id,
                expected_input_set_hash, computed_input_set_hash, hash_specification_version,
                retry_policy_version, target_version_set, input_identity, deletion_generation,
                status, request_payload from trading_review_jobs where job_id=%s::uuid""", (job_id,))
            require(job is not None, "COMPUTE_JOB_NOT_PERSISTED")
            identity = run["input_identity_json"]
            require(identity == job["input_identity"], "INPUT_IDENTITY_DRIFT")
            require(identity["tradingBookId"] == book["id"] and
                    identity["sourceImportSessionIds"] == [session_id], "SOURCE_IDENTITY_DRIFT")
            require({r: v["artifactId"] for r, v in identity["artifactsByRole"].items()} ==
                    {r: uploaded[r]["id"] for r in artifacts}, "ARTIFACT_IDENTITY_DRIFT")
            require(run_id == job["core_normalization_run_id"], "CORE_RUN_ID_DRIFT")
            require(run["input_fingerprint"] == job["expected_input_set_hash"] ==
                    job["computed_input_set_hash"], "INPUT_SET_HASH_DRIFT")
            require(run["target_version_set_json"] == job["target_version_set"] ==
                    identity["targetVersionSet"], "VERSION_SET_DRIFT")
            require(job["hash_specification_version"] == "TRADING_INPUT_SET_HASH_V1" and
                    job["deletion_generation"] == run["deletion_generation"] and
                    job["retry_policy_version"] == run["retry_policy_version"], "RUN_METADATA_DRIFT")
            original = job["request_payload"]  # Compute deliberately removes signed locators.
            require(set(original["inputIdentity"]) == set(identity), "SERIALIZER_IDENTITY_SHAPE_DRIFT")
            require("artifactAccessGrants" not in original["inputIdentity"] and
                    len(original["artifactAccessGrants"]) == 2, "GRANTS_IN_LOGICAL_IDENTITY")
            status = poll(lambda: request(compute, "GET",
                f"{args.compute_url}/trading-review/normalization-jobs/{job_id}", 200),
                lambda s: s["status"] in {"COMPLETED", "FAILED", "CANCELLED"},
                args.timeout, "Compute terminal")
            require(status["status"] == "COMPLETED", str(status.get("failure", {}).get("code", "JOB_NOT_COMPLETED")))
            descriptor = request(compute, "GET",
                f"{args.compute_url}/trading-review/normalization-jobs/{job_id}/result", 200)
            resource_url = f"{args.compute_url}/trading-review/terminal-payloads/{descriptor['payloadId']}"
            if args.result_integrity:
                raw_response = compute.get(resource_url, headers={"Accept-Encoding": "identity"})
                require(raw_response.status_code == 200, "TERMINAL_RESOURCE_UNAVAILABLE")
                try:
                    payload = verify_transport(raw_response.content, descriptor,
                                               raw_response.headers.get("Content-Type", ""),
                                               raw_response.headers.get("Content-Encoding", ""))
                except IntegrityFailure as error:
                    raise ScenarioFailure(error.code) from None
            else:
                payload = request(compute, "GET", resource_url, 200)
            check_schema(args.context_path, payload)
            require(run_id == status["coreNormalizationRunId"] == descriptor["coreNormalizationRunId"] ==
                    payload["coreNormalizationRunId"], "TERMINAL_RUN_ID_DRIFT")
            require(run["input_fingerprint"] == status["inputSetHash"] == descriptor["inputSetHash"] ==
                    payload["inputSetHash"], "TERMINAL_INPUT_HASH_DRIFT")
            require(run["target_version_set_json"] == status["targetVersionSet"] ==
                    descriptor["targetVersionSet"] == payload["targetVersionSet"], "TERMINAL_VERSION_SET_DRIFT")
            require(run["deletion_generation"] == status["deletionGeneration"] ==
                    descriptor["deletionGeneration"] == payload["deletionGeneration"],
                    "TERMINAL_GENERATION_DRIFT")
            require(payload["hashSpecificationVersion"] ==
                    descriptor["hashSpecificationVersion"] ==
                    job["hash_specification_version"], "TERMINAL_HASH_SPECIFICATION_DRIFT")
            require(descriptor["resultHash"] == payload["resultHash"] and
                    descriptor["reconciliationManifestHash"] ==
                    payload["reconciliationManifest"]["manifestHash"], "RESULT_HASH_DRIFT")
            if args.result_integrity:
                scenario = "result_integrity"
                scenario_started = time.monotonic()
                try:
                    schema_validate(args.context_path, payload, descriptor)
                    result = verify(payload, descriptor,
                                    {role: uploaded[role]["id"] for role in artifacts},
                                    {role: artifacts[role]["sha256"] for role in artifacts})
                    evidence["resultIntegrity"] = {
                        "transportBytes": len(raw_response.content),
                        "transportHashPrefix": descriptor["payloadSha256"][:12],
                        "counts": result["counts"],
                        "sectionHashPrefixes": {k: v[:12] for k, v in result["sectionHashes"].items()},
                        "manifestHashPrefix": result["manifestHash"][:12],
                        "resultHashPrefix": result["resultHash"][:12],
                        "evidenceReferences": "PASS",
                    }
                    evidence["pagination"] = pagination_independence(
                        payload, {role: uploaded[role]["id"] for role in artifacts})
                    resource_id = descriptor["payloadId"]
                    require(collect_v1_resource([(resource_id, raw_response.content),
                                                 (resource_id, raw_response.content)], resource_id)
                            == raw_response.content, "DUPLICATE_RESOURCE_DRIFT")
                    for name, pages, expected in (
                        ("missingResource", [], "RESULT_COUNT_MISMATCH"),
                        ("conflictingResource", [(resource_id, raw_response.content),
                                                 (resource_id, raw_response.content + b" ")], "RESULT_ITEM_CONFLICT"),
                    ):
                        try:
                            collect_v1_resource(pages, resource_id)
                        except IntegrityFailure as error:
                            require(error.code == expected, "PAGE_COLLECTION_WRONG_FAILURE")
                        else:
                            raise ScenarioFailure("PAGE_COLLECTION_NOT_DETECTED")
                    evidence["pagination"].update(duplicateResource="PASS", missingResource="PASS",
                                                   conflictingResource="PASS")
                    evidence["tamper"] = negative_scenarios(
                        raw_response.content, descriptor, payload,
                        {role: uploaded[role]["id"] for role in artifacts})
                except IntegrityFailure as error:
                    evidence["integrityFailure"] = {"section": error.section,
                                                    "expectedPrefix": error.expected,
                                                    "actualPrefix": error.actual}
                    raise ScenarioFailure(error.code) from None
                mark_pass(evidence, scenario, scenario_started)
                scenario = "primary"
            mark_pass(evidence, "primary", scenario_started)
            evidence["inputSetHash"] = run["input_fingerprint"]
            evidence["versionSetHash"] = run["target_version_set_json"]["versionSetHash"]
            evidence["resultHash"] = descriptor["resultHash"]

            scenario = "source_rejection_quality_null"
            scenario_started = time.monotonic()
            rejected_book = request(core, "POST",
                f"{args.core_url}/trading-accounts/{account['id']}/books", 201,
                headers={"Idempotency-Key": f"rejected-book-{uuid.uuid4().hex}"},
                json={"displayName": "Synthetic rejection", "productFamily": "LINEAR_PERPETUAL",
                      "positionMode": "ONE_WAY", "settlementAsset": "USDT", "reviewTimezone": "UTC"})
            book_id = int(rejected_book["id"])
            before = row(connection, """select latest_ledger_revision_id::text as latest
                from trading_books where id=%s""", (book_id,))
            require(before is not None, "REJECTION_BOOK_MISSING")
            rejected_session = request(core, "POST",
                f"{args.core_url}/trading-books/{book_id}/imports", 201,
                headers={"Idempotency-Key": f"rejected-import-{uuid.uuid4().hex}"},
                json={"sourceTimezone": "Asia/Seoul", "reviewTimezone": "UTC",
                      "declaredPositionMode": "ONE_WAY"})
            rejected_session_id = rejected_session["id"]
            evidence["rejectedSessionId"] = rejected_session_id
            missing_column_path = args.run_dir / "synthetic-trade-missing-maker.csv"
            missing_column_sha = missing_maker_column(
                args.fixture_root / artifacts["TRADE_HISTORY"]["path"], missing_column_path)
            upload(core, args.core_url, rejected_session_id, "TRADE_HISTORY",
                   missing_column_path, missing_column_sha)
            position = artifacts["POSITION_HISTORY"]
            upload(core, args.core_url, rejected_session_id, "POSITION_HISTORY",
                   args.fixture_root / position["path"], position["sha256"])
            validation = request(core, "POST",
                f"{args.core_url}/trading-import-sessions/{rejected_session_id}/validation", 202,
                headers={"Idempotency-Key": f"rejected-validate-{uuid.uuid4().hex}"}, json={})
            require(validation["status"] == "VALIDATING", "REJECTION_NOT_DISPATCHED")
            terminal = poll(lambda: request(core, "GET",
                f"{args.core_url}/trading-import-sessions/{rejected_session_id}", 200),
                lambda value: value["status"] in {"REJECTED", "PROCESSING_FAILED", "ACCEPTED"},
                args.timeout, scenario)
            require(terminal["status"] == "REJECTED", "SOURCE_NOT_REJECTED")
            require(terminal.get("failure", {}).get("code") == "REQUIRED_COLUMN_MISSING",
                    "SOURCE_FAILURE_CODE_MISMATCH")
            require("qualitySummary" in terminal and terminal["qualitySummary"] is None,
                    "REJECTED_QUALITY_NOT_EXPLICIT_NULL")
            require(terminal.get("completedAt") is not None and
                    terminal.get("createdLedgerRevisionId") is None,
                    "REJECTED_PUBLIC_SHAPE_INVALID")
            rejected_run = row(connection, """select id::text, compute_job_id, status,
                last_failure_code from trading_normalization_runs where import_session_id=%s
                order by created_at desc limit 1""", (int(rejected_session_id),))
            require(rejected_run is not None and rejected_run["status"] == "TERMINAL_FAILURE" and
                    rejected_run["compute_job_id"] is not None, "REJECTION_RUN_NOT_TERMINAL")
            failed_job_id = rejected_run["compute_job_id"]
            failed_job = request(compute, "GET",
                f"{args.compute_url}/trading-review/normalization-jobs/{failed_job_id}", 200)
            require(failed_job["status"] == "FAILED" and
                    failed_job.get("failure", {}).get("code") == "REQUIRED_COLUMN_MISSING" and
                    failed_job.get("terminalPayloadId") is None and
                    failed_job.get("qualityReport") is None, "COMPUTE_FAILURE_SHAPE_INVALID")
            require(compute.get(f"{args.compute_url}/trading-review/normalization-jobs/{failed_job_id}/result")
                    .status_code == 409, "FAILED_RESULT_AVAILABLE")
            after = row(connection, """select latest_ledger_revision_id::text as latest
                from trading_books where id=%s""", (book_id,))
            require(after is not None and after["latest"] == before["latest"],
                    "REJECTED_LATEST_CHANGED")
            revision = row(connection, """select count(*) as total from trading_ledger_revisions
                where trading_book_id=%s""", (book_id,))
            ack = row(connection, """select count(*) as total from trading_normalization_ack_dispatches
                where core_import_session_id=%s""", (int(rejected_session_id),))
            require(revision is not None and revision["total"] == 0 and
                    ack is not None and ack["total"] == 0, "REJECTED_REVISION_OR_ACK_CREATED")
            retention = row(connection, """select count(*) as total from trading_source_artifacts
                where import_session_id=%s and retention_status='ACTIVE' and
                raw_expires_at > CURRENT_TIMESTAMP""", (int(rejected_session_id),))
            require(retention is not None and retention["total"] == 2,
                    "REJECTED_RAW_RETENTION_CHANGED")
            evidence.setdefault("safeFailureCodes", {})[scenario] = "REQUIRED_COLUMN_MISSING"
            mark_pass(evidence, scenario, scenario_started)

            if args.result_integrity:
                scenario = "persistence_restart"
                scenario_started = time.monotonic()
                scripts = args.test_root / "scripts"
                process_script(scripts / "stop-integration.ps1", "-RegistryId", args.registry,
                               "-Service", "compute-api")
                process_script(scripts / "start-compute-api.ps1", "-EnvironmentFile",
                               str(args.environment_file), "-ComputeApiPath", str(args.compute_path),
                               "-Schema", args.compute_schema, "-Profile", args.registry,
                               "-RegistryId", args.registry)
                poll(lambda: health_code(compute, f"{args.compute_url}/health"),
                     lambda code: code == 200, args.timeout, "Compute API restart")
                after_descriptor = request(compute, "GET",
                    f"{args.compute_url}/trading-review/normalization-jobs/{job_id}/result", 200)
                after_response = compute.get(resource_url, headers={"Accept-Encoding": "identity"})
                require(after_response.status_code == 200, "RESTART_RESOURCE_UNAVAILABLE")
                try:
                    after_payload = verify_transport(after_response.content, after_descriptor,
                        after_response.headers.get("Content-Type", ""),
                        after_response.headers.get("Content-Encoding", ""))
                    verify(after_payload, after_descriptor,
                           {role: uploaded[role]["id"] for role in artifacts},
                           {role: artifacts[role]["sha256"] for role in artifacts})
                except IntegrityFailure as error:
                    raise ScenarioFailure(error.code) from None
                require(after_descriptor == descriptor and after_response.content == raw_response.content,
                        "RESTART_RESULT_DRIFT")
                evidence["restartIntegrity"] = "PASS"
                mark_pass(evidence, scenario, scenario_started)
                return_code = 0
                return 0

            scenario = "hash_mismatch"
            scenario_started = time.monotonic()
            bad = negative_request(original, args.core_url)
            bad["coreLogicalJobId"] = str(uuid.uuid4())
            bad["coreNormalizationRunId"] = str(uuid.uuid4())
            bad["attemptToken"] = str(uuid.uuid4())
            bad["expectedInputSetHash"] = "0" * 64 if run["input_fingerprint"] != "0" * 64 else "1" * 64
            accepted = request(compute, "POST", f"{args.compute_url}/trading-review/normalization-jobs", 202,
                               headers={"Idempotency-Key": f"negative-{uuid.uuid4().hex}"}, json=bad)
            failed = poll(lambda: request(compute, "GET", f"{args.compute_url}/trading-review/normalization-jobs/{accepted['jobId']}", 200),
                          lambda s: s["status"] in {"FAILED", "COMPLETED"}, args.timeout, scenario)
            require(failed["status"] == "FAILED" and failed["failure"]["code"] == "INPUT_SET_HASH_MISMATCH"
                    and failed["coreNormalizationRunId"] == bad["coreNormalizationRunId"],
                    "INPUT_SET_HASH_MISMATCH_FAILURE_DRIFT")
            assert_no_result(compute, args.compute_url, accepted["jobId"])
            evidence.setdefault("safeFailureCodes", {})[scenario] = failed["failure"]["code"]
            mark_pass(evidence, scenario, scenario_started)

            scenario = "persistence_restart"
            scenario_started = time.monotonic()
            scripts = args.test_root / "scripts"
            process_script(scripts / "stop-integration.ps1", "-RegistryId", args.registry,
                           "-Service", "compute-api")
            process_script(scripts / "start-compute-api.ps1", "-EnvironmentFile",
                           str(args.environment_file), "-ComputeApiPath", str(args.compute_path),
                           "-Schema", args.compute_schema, "-Profile", args.registry,
                           "-RegistryId", args.registry)
            poll(lambda: health_code(compute, f"{args.compute_url}/health"),
                 lambda code: code == 200, args.timeout, "Compute API restart")
            restarted = request(compute, "GET",
                                f"{args.compute_url}/trading-review/normalization-jobs/{job_id}", 200)
            require(restarted["status"] == "COMPLETED" and
                    restarted["coreNormalizationRunId"] == run_id and
                    restarted["inputSetHash"] == run["input_fingerprint"] and
                    restarted["targetVersionSet"] == run["target_version_set_json"] and
                    restarted["deletionGeneration"] == run["deletion_generation"],
                    "RESTART_IDENTITY_DRIFT")
            persisted_after_restart = row(compute_db, """select retry_policy_version,
                hash_specification_version, target_version_set, deletion_generation
                from trading_review_jobs where job_id=%s::uuid""", (job_id,))
            require(persisted_after_restart["retry_policy_version"] == run["retry_policy_version"] and
                    persisted_after_restart["hash_specification_version"] == job["hash_specification_version"] and
                    persisted_after_restart["target_version_set"] == run["target_version_set_json"] and
                    persisted_after_restart["deletion_generation"] == run["deletion_generation"],
                    "RESTART_PERSISTED_METADATA_DRIFT")
            replay = request(compute, "POST", f"{args.compute_url}/trading-review/normalization-jobs",
                             202, headers={"Idempotency-Key": dispatch["compute_key"]},
                             json=negative_request(original, args.core_url))
            require(replay["jobId"] == job_id, "RESTART_IDEMPOTENCY_DRIFT")
            mark_pass(evidence, scenario, scenario_started)

            scenario = "source_content_hash_mismatch"
            scenario_started = time.monotonic()
            trade = (args.fixture_root / artifacts["TRADE_HISTORY"]["path"]).read_bytes()
            changed = bytearray(trade)
            changed[-2] = ord("0") if changed[-2] != ord("0") else ord("1")
            position = (args.fixture_root / artifacts["POSITION_HISTORY"]["path"]).read_bytes()
            fault_server = source_fault_server(args.fixture_port, {
                "/TRADE_HISTORY": bytes(changed), "/POSITION_HISTORY": position})
            bad = negative_request(original, args.core_url)
            bad["coreLogicalJobId"] = str(uuid.uuid4())
            bad["coreNormalizationRunId"] = str(uuid.uuid4())
            bad["attemptToken"] = str(uuid.uuid4())
            for grant in bad["artifactAccessGrants"]:
                grant["accessUri"] = f"http://127.0.0.1:{args.fixture_port}/{grant['role']}"
            accepted = request(compute, "POST", f"{args.compute_url}/trading-review/normalization-jobs", 202,
                               headers={"Idempotency-Key": f"negative-{uuid.uuid4().hex}"}, json=bad)
            failed = poll(lambda: request(compute, "GET", f"{args.compute_url}/trading-review/normalization-jobs/{accepted['jobId']}", 200),
                          lambda s: s["status"] in {"FAILED", "COMPLETED"}, args.timeout, scenario)
            require(failed["status"] == "FAILED" and
                    failed["failure"]["code"] == "SOURCE_CONTENT_HASH_MISMATCH" and
                    failed["coreNormalizationRunId"] == bad["coreNormalizationRunId"],
                    "SOURCE_CONTENT_HASH_MISMATCH_FAILURE_DRIFT")
            assert_no_result(compute, args.compute_url, accepted["jobId"])
            evidence.setdefault("safeFailureCodes", {})[scenario] = failed["failure"]["code"]
            mark_pass(evidence, scenario, scenario_started)
            fault_server.shutdown()
            fault_server.server_close()
            fault_server = None

            scenario = "unsupported_hash_version"
            scenario_started = time.monotonic()
            bad = negative_request(original, args.core_url)
            bad["coreLogicalJobId"] = str(uuid.uuid4())
            bad["coreNormalizationRunId"] = str(uuid.uuid4())
            bad["attemptToken"] = str(uuid.uuid4())
            bad["inputIdentity"]["hashSpecificationVersion"] = "TRADING_INPUT_SET_HASH_FUTURE"
            response = compute.post(f"{args.compute_url}/trading-review/normalization-jobs",
                                    headers={"Idempotency-Key": f"negative-{uuid.uuid4().hex}"}, json=bad)
            if response.status_code == 202:
                failed = poll(lambda: request(compute, "GET", f"{args.compute_url}/trading-review/normalization-jobs/{response.json()['jobId']}", 200),
                              lambda s: s["status"] in {"FAILED", "COMPLETED"}, args.timeout, scenario)
                require(failed["status"] == "FAILED" and
                        failed["failure"]["code"] == "INPUT_HASH_SPECIFICATION_UNSUPPORTED" and
                        failed["coreNormalizationRunId"] == bad["coreNormalizationRunId"] and
                        failed["failure"]["retryable"] is False,
                        "HASH_SPECIFICATION_FAILURE_DRIFT")
                assert_no_result(compute, args.compute_url, response.json()["jobId"])
                evidence.setdefault("safeFailureCodes", {})[scenario] = failed["failure"]["code"]
            else:
                require(response.status_code in {400, 422} and
                        response.json().get("code") == "INPUT_HASH_SPECIFICATION_UNSUPPORTED",
                        "HASH_SPECIFICATION_FAILURE_DRIFT")
                evidence.setdefault("safeFailureCodes", {})[scenario] = response.json()["code"]
            mark_pass(evidence, scenario, scenario_started)

            scenario = "idempotency_conflict"
            scenario_started = time.monotonic()
            bad = negative_request(original, args.core_url)
            bad["coreNormalizationRunId"] = str(uuid.uuid4())
            response = compute.post(f"{args.compute_url}/trading-review/normalization-jobs",
                                    headers={"Idempotency-Key": dispatch["compute_key"]}, json=bad)
            require(response.status_code == 409 and response.json().get("code") in
                    {"IDEMPOTENCY_CONFLICT", "IDEMPOTENCY_PAYLOAD_CONFLICT"}, "IDEMPOTENCY_CONFLICT_MISSING")
            evidence.setdefault("safeFailureCodes", {})[scenario] = response.json()["code"]
            same = row(compute_db, "select core_normalization_run_id, expected_input_set_hash from trading_review_jobs where job_id=%s::uuid", (job_id,))
            require(same["core_normalization_run_id"] == run_id and
                    same["expected_input_set_hash"] == run["input_fingerprint"], "IDEMPOTENCY_MUTATED_FIRST_JOB")
            mark_pass(evidence, scenario, scenario_started)

            scenario = "locator_refresh"
            scenario_started = time.monotonic()
            scripts = args.test_root / "scripts"
            process_script(scripts / "stop-integration.ps1", "-RegistryId", args.registry,
                           "-Service", "compute-worker")
            refresh_session = request(core, "POST", f"{args.core_url}/trading-books/{book['id']}/imports", 201,
                                      headers={"Idempotency-Key": f"import-{uuid.uuid4().hex}"},
                                      json={"sourceTimezone": "Asia/Seoul", "reviewTimezone": "UTC",
                                            "declaredPositionMode": "ONE_WAY"})
            refresh_session_id = refresh_session["id"]
            evidence["refreshSessionId"] = refresh_session_id
            for role in ("TRADE_HISTORY", "POSITION_HISTORY"):
                item = artifacts[role]
                upload(core, args.core_url, refresh_session_id, role,
                       args.fixture_root / item["path"], item["sha256"])
            request(core, "POST", f"{args.core_url}/trading-import-sessions/{refresh_session_id}/validation",
                    202, headers={"Idempotency-Key": f"validate-{uuid.uuid4().hex}"}, json={})
            refresh_run = poll(lambda: row(connection, """select id::text, input_fingerprint,
                logical_key, target_version_set_json, compute_job_id from trading_normalization_runs
                where import_session_id=%s order by created_at desc limit 1""", (int(refresh_session_id),)),
                lambda r: r["compute_job_id"] is not None, args.timeout, "refresh Core dispatch")
            refresh_job_id = refresh_run["compute_job_id"]
            refresh_dispatch = row(connection, """select compute_key, status from
                trading_normalization_dispatches where run_id=%s::uuid""", (refresh_run["id"],))
            require(refresh_dispatch is not None and refresh_dispatch["status"] in
                    {"SUBMITTED", "RESULT_READY"}, "REFRESH_DISPATCH_MISSING")
            evidence["refreshCoreRunId"] = refresh_run["id"]
            evidence["refreshComputeJobId"] = refresh_job_id
            pending = row(compute_db, """select job_id::text, core_normalization_run_id,
                expected_input_set_hash, computed_input_set_hash, request_payload, status,
                grant_revision, input_identity, target_version_set, deletion_generation
                from trading_review_jobs where job_id=%s::uuid""", (refresh_job_id,))
            require(pending is not None and pending["status"] == "PENDING" and
                    pending["grant_revision"] == 0, "REFRESH_JOB_NOT_PENDING")
            # Integration DB fault injection: expire only Compute's first grant revision
            # while the worker is stopped. Core must issue real replacement grants.
            with compute_db.cursor() as cursor:
                cursor.execute("""update trading_review_grant_revisions
                    set grant_expires_at=CURRENT_TIMESTAMP - interval '1 second'
                    where job_id=%s::uuid and grant_revision=0 and status='ACTIVE'""",
                    (refresh_job_id,))
                require(cursor.rowcount == 2, "REFRESH_FAULT_INJECTION_FAILED")
            refreshed = poll(lambda: request(compute, "GET",
                f"{args.compute_url}/trading-review/normalization-jobs/{refresh_job_id}", 200),
                lambda s: s.get("grantRevision", 0) >= 1 or s["status"] in {"FAILED", "CANCELLED"},
                args.timeout, "Core grant refresh")
            require(refreshed["status"] == "PENDING" and refreshed["grantRevision"] == 1,
                    "GRANT_REFRESH_FAILED")
            after = row(compute_db, """select core_normalization_run_id,
                expected_input_set_hash, computed_input_set_hash, input_identity,
                target_version_set, deletion_generation, request_payload, status,
                grant_revision from trading_review_jobs where job_id=%s::uuid""", (refresh_job_id,))
            require(after["core_normalization_run_id"] == refresh_run["id"] ==
                    pending["core_normalization_run_id"] and
                    after["expected_input_set_hash"] == refresh_run["input_fingerprint"] ==
                    pending["expected_input_set_hash"] and
                    after["computed_input_set_hash"] == pending["computed_input_set_hash"] and
                    after["input_identity"] == pending["input_identity"] and
                    after["target_version_set"] == pending["target_version_set"] and
                    after["deletion_generation"] == pending["deletion_generation"] and
                    after["request_payload"]["inputIdentity"] ==
                    pending["request_payload"]["inputIdentity"] and
                    all("accessUri" not in grant for grant in
                        after["request_payload"]["artifactAccessGrants"]),
                    "LOCATOR_REFRESH_CHANGED_LOGICAL_IDENTITY")
            grant_counts = row(compute_db, """select
                count(*) filter (where grant_revision=0 and status='EXPIRED') as expired,
                count(*) filter (where grant_revision=1 and status='ACTIVE') as active
                from trading_review_grant_revisions where job_id=%s::uuid""", (refresh_job_id,))
            require(grant_counts == {"expired": 2, "active": 2},
                    "GRANT_REVISION_NOT_REPLACED")
            same_dispatch = row(connection, """select compute_key from
                trading_normalization_dispatches where run_id=%s::uuid""", (refresh_run["id"],))
            same_run = row(connection, """select logical_key, input_fingerprint from
                trading_normalization_runs where id=%s::uuid""", (refresh_run["id"],))
            require(same_dispatch["compute_key"] == refresh_dispatch["compute_key"] and
                    same_run["logical_key"] == refresh_run["logical_key"] and
                    same_run["input_fingerprint"] == refresh_run["input_fingerprint"],
                    "LOCATOR_REFRESH_CHANGED_IDEMPOTENCY")
            process_script(scripts / "start-compute-worker.ps1", "-EnvironmentFile",
                           str(args.environment_file), "-ComputeApiPath", str(args.compute_path),
                           "-Schema", args.compute_schema, "-Profile", args.registry,
                           "-RegistryId", args.registry, "-TradingReview")
            refresh_terminal = poll(lambda: request(compute, "GET",
                f"{args.compute_url}/trading-review/normalization-jobs/{refresh_job_id}", 200),
                lambda s: s["status"] in {"COMPLETED", "FAILED", "CANCELLED"},
                args.timeout, "refreshed job terminal")
            require(refresh_terminal["status"] == "COMPLETED" and
                    refresh_terminal["coreNormalizationRunId"] == refresh_run["id"] and
                    refresh_terminal["inputSetHash"] == refresh_run["input_fingerprint"],
                    "REFRESH_TERMINAL_IDENTITY_DRIFT")
            refresh_descriptor = request(compute, "GET",
                f"{args.compute_url}/trading-review/normalization-jobs/{refresh_job_id}/result", 200)
            refresh_payload = request(compute, "GET",
                f"{args.compute_url}/trading-review/terminal-payloads/{refresh_descriptor['payloadId']}", 200)
            check_schema(args.context_path, refresh_payload)
            require(refresh_payload["coreNormalizationRunId"] == refresh_run["id"] and
                    refresh_payload["inputSetHash"] == refresh_run["input_fingerprint"] and
                    refresh_payload["targetVersionSet"] == refresh_run["target_version_set_json"],
                    "REFRESH_PAYLOAD_IDENTITY_DRIFT")
            mark_pass(evidence, scenario, scenario_started)

            scenario = "sensitive_log_audit"
            scenario_started = time.monotonic()
            raw_sources = [
                (args.fixture_root / artifacts[role]["path"]).read_text(encoding="utf-8")
                for role in ("TRADE_HISTORY", "POSITION_HISTORY")
            ]
            for log_path in evidence["logPaths"].values():
                data = Path(log_path).read_text(encoding="utf-8", errors="replace")
                require("/internal/trading-review/artifact-grants/" not in data and
                        all(source not in data for source in raw_sources),
                        "SENSITIVE_LOG_EXPOSURE")
            mark_pass(evidence, scenario, scenario_started)
    except Exception as error:  # noqa: BLE001 - always write safe failure evidence
        evidence["scenarios"][scenario] = "FAIL"
        evidence["safeFailureCode"] = error.code if isinstance(error, (ScenarioFailure, IntegrityFailure)) else type(error).__name__
        evidence["lastState"] = str(error) if isinstance(error, ScenarioFailure) else type(error).__name__
        return_code = 1
    else:
        return_code = 0
    finally:
        if fault_server is not None:
            fault_server.shutdown()
            fault_server.server_close()
        if connection is not None:
            connection.close()
        if compute_db is not None:
            compute_db.close()
        evidence["durationSeconds"] = round(time.monotonic() - started, 3)
        evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Scenario {scenario}: {'PASS' if return_code == 0 else 'FAIL'}; evidence={evidence_path}")
        if return_code:
            print(f"Safe failure code: {evidence['safeFailureCode']}")
    return return_code


if __name__ == "__main__":
    sys.exit(main())
