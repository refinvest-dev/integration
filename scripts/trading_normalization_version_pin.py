"""Integration-owned pin observations, HTTP faults and pre-pin migration rehearsal.

The primary producer is always the real Core dispatcher and Compute worker.
Fault responses are scoped to new synthetic sessions; no service code is patched.
Only allowlisted observations leave memory. Signed grants and bodies are never logged.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import psycopg
from psycopg import sql


SCHEMA = "resultSchemaVersion"
HASH = "resultHashSpecificationVersion"
V1 = ("TRADING_NORMALIZATION_RESULT_V1", "TRADING_NORMALIZATION_RESULT_HASH_V1")
V2 = ("TRADING_NORMALIZATION_RESULT_V2", "TRADING_NORMALIZATION_RESULT_HASH_V2")


def pair(value):
    return value.get(SCHEMA), value.get(HASH)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def fingerprint(value):
    """Pre-pin producer fingerprint, also checked against the migrated HTTP replay."""
    value = copy.deepcopy(value)
    for grant in value["artifactAccessGrants"]:
        grant.pop("accessUri", None)
        grant.pop("expiresAt", None)
    value["artifactAccessGrants"].sort(key=lambda item: item["role"])
    value["pinnedInstrumentTerms"].sort(key=lambda item: item["instrumentId"])
    return hashlib.sha256(encoded(value)).hexdigest()


class PinProxy:
    def __init__(self, runtime, args):
        self.rt, self.args = runtime, args
        self.mode = None
        self.faults = {}
        self.creates = {}
        self.acks = []
        self.errors = []
        self.transport_failures = 0
        self.reject_acks = True
        self.injected_ack_failures = 0
        self.core_schema = "public"
        self.create_attempts = []
        self.reject_create_sessions = set()
        self.drop_response_sessions = set()
        self.block_create_session = None
        self.block_entered = threading.Event()
        self.block_release = threading.Event()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                try:
                    if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                        chunks = []
                        while True:
                            size = int(self.rfile.readline().strip().split(b";", 1)[0], 16)
                            if size == 0:
                                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                                    pass
                                break
                            chunks.append(self.rfile.read(size))
                            runtime.require(self.rfile.read(2) == b"\r\n", "PROXY_CHUNK_INVALID")
                        body = b"".join(chunks)
                    else:
                        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    headers = {k: v for k, v in self.headers.items()
                               if k.lower() not in {"host", "connection", "content-length", "accept-encoding", "transfer-encoding"}}
                    headers["Accept-Encoding"] = "identity"
                    is_create = self.command == "POST" and self.path.endswith("/normalization-jobs")
                    if is_create:
                        sent = json.loads(body)
                        sid = str(sent["coreImportSessionId"])
                        proxy.create_attempts.append({"sessionId": sid,
                            "runId": sent["coreNormalizationRunId"], "at": time.monotonic()})
                        if sid in proxy.reject_create_sessions:
                            fault_body = encoded({"code": "TRADING_REVIEW_CAPACITY_EXCEEDED",
                                "message": "Synthetic capacity fault", "traceId": uuid.uuid4().hex, "retryable": True})
                            self.send_response(503)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Retry-After", "1")
                            self.send_header("Content-Length", str(len(fault_body)))
                            self.end_headers()
                            self.wfile.write(fault_body)
                            return
                        if sid == proxy.block_create_session:
                            proxy.block_entered.set()
                            runtime.require(proxy.block_release.wait(12), "CREATE_BARRIER_TIMEOUT")
                    if self.command == "POST" and self.path.endswith("/acknowledgement"):
                        ack = json.loads(body)
                        # A separate connection must see the committed Revision before HTTP ACK.
                        with runtime.db_connection(proxy.core_schema) as db:
                            observed = runtime.row(db, """select r.id::text as revision_id,n.id::text as run_id,
                                n.status as run_status from trading_ledger_revisions r
                                join trading_normalization_runs n on n.id=r.run_id
                                join trading_normalization_ack_dispatches a on a.run_id=n.id
                                where a.payload_id=%s""", (ack["payloadId"],))
                        reject_ack = bool(proxy.reject_acks and observed and observed["run_id"] in proxy.creates)
                        proxy.acks.append({"payloadId": ack["payloadId"],
                                           "disposition": ack.get("disposition"),
                                           "injectedFailure": reject_ack,
                                           "commitVisible": bool(observed and observed["run_status"] == "PUBLISHED"),
                                           "revisionId": observed["revision_id"] if observed else None})
                        if reject_ack:
                            proxy.injected_ack_failures += 1
                            self.send_response(503)
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                    with httpx.Client(timeout=20, trust_env=False) as upstream:
                        response = upstream.request(self.command, args.compute_url + self.path,
                                                    headers=headers, content=body)
                    data = response.content
                    if response.status_code == 202 and self.command == "POST" and self.path.endswith("/normalization-jobs"):
                        sent, accepted = json.loads(body), response.json()
                        proxy.creates[sent["coreNormalizationRunId"]] = (sent, accepted)
                        if proxy.mode and str(sent["coreImportSessionId"]) == proxy.mode[1]:
                            proxy.faults[accepted["jobId"]] = {"mode": proxy.mode[0],
                                "sessionId": sent["coreImportSessionId"],
                                "runId": sent["coreNormalizationRunId"], "payloadId": None,
                                "descriptorReads": 0, "payloadReads": 0}
                            proxy.mode = None
                        if str(sent["coreImportSessionId"]) in proxy.drop_response_sessions:
                            # Real Compute accepted; the caller receives no HTTP response.
                            self.close_connection = True
                            return
                    if response.status_code == 200 and self.path.endswith("/result"):
                        job_id = self.path.split("/")[-2]
                        fault = proxy.faults.get(job_id)
                        if fault:
                            descriptor = response.json()
                            fault["payloadId"] = descriptor["payloadId"]
                            fault["descriptorReads"] += 1
                            if fault["mode"] == "descriptor":
                                descriptor.update({SCHEMA: V2[0], HASH: V2[1]})
                            else:
                                with httpx.Client(timeout=20, trust_env=False) as upstream:
                                    payload_response = upstream.get(args.compute_url +
                                        "/trading-review/terminal-payloads/" + descriptor["payloadId"], headers=headers)
                                runtime.require(payload_response.status_code == 200, "FAULT_PAYLOAD_UNAVAILABLE")
                                payload = payload_response.json()
                                payload.update({SCHEMA: V2[0], HASH: V2[1]})
                                fault["body"] = encoded(payload)
                                descriptor["byteSize"] = len(fault["body"])
                                descriptor["payloadSha256"] = hashlib.sha256(fault["body"]).hexdigest()
                            data = encoded(descriptor)
                    for fault in proxy.faults.values():
                        if fault["mode"] == "payload" and self.path == "/trading-review/terminal-payloads/" + str(fault["payloadId"]):
                            data = fault["body"]
                            fault["payloadReads"] += 1
                    self.send_response(response.status_code)
                    self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as error:
                    if isinstance(error, httpx.HTTPError):
                        # Publication deliberately stops the upstream API to exercise ACK retry.
                        proxy.transport_failures += 1
                    else:
                        proxy.errors.append(type(error).__name__)
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", args.fixture_port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def evidence(self):
        return {"ackRequests": self.acks, "errors": self.errors,
                "createAttempts": self.create_attempts,
                "upstreamUnavailableRequests": self.transport_failures,
                "injectedAckFailures": self.injected_ack_failures,
                "creates": {run: {"jobId": accepted["jobId"], "requestPair": pair(sent),
                                  "acceptedPair": pair(accepted)}
                            for run, (sent, accepted) in self.creates.items()},
                "faults": {job: {k: v for k, v in fault.items() if k != "body"}
                           for job, fault in self.faults.items()}}

    def diagnosis(self):
        values = {}
        with self.rt.db_connection("public") as core, self.rt.db_connection(self.args.compute_schema) as compute:
            for job, fault in self.faults.items():
                values[job] = self.rt.publication_diagnosis(core, compute, {
                    "coreRunId": fault["runId"], "coreSessionId": fault["sessionId"], "computeJobId": job})
        return values

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def verify_version_pin(rt, args, evidence, core, compute, db, compute_db, proxy, book, account,
                       session_id, run_id, job_id, dispatch, original, status, descriptor,
                       payload, artifacts, validation_key):
    checks = evidence["versionPin"]
    from trading_normalization_cutover import worker_poll

    def begin(name):
        checks["activeScenario"] = name
        checks["scenarios"][name] = {"result": "RUNNING"}

    def passed(name, **values):
        checks["scenarios"][name] = {"result": "PASS", **values}
        checks.pop("activeScenario", None)
        # Preserve completed scenarios even if a later service restart fails.
        (args.run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8")

    def stored_job():
        return rt.row(compute_db, "select * from trading_review_jobs where job_id=%s::uuid", (job_id,))

    begin("V1_PIN_PUBLICATION")
    pinned_run = rt.row(db, """select result_schema_version,result_hash_specification_version
        from trading_normalization_runs where id=%s::uuid""", (run_id,))
    pinned_dispatch = rt.row(db, """select result_schema_version,result_hash_specification_version
        from trading_normalization_dispatches where run_id=%s::uuid""", (run_id,))
    job = stored_job()
    sent, accepted = proxy.creates[run_id]
    for value in (pinned_run, pinned_dispatch, job):
        rt.require((value["result_schema_version"], value["result_hash_specification_version"]) == V1,
                   "PERSISTED_PIN_DRIFT")
    for value in (sent, accepted, original, status, descriptor, payload, payload["resultDigestManifest"]):
        rt.require(pair(value) == V1, "HTTP_PIN_DRIFT")
    rt.require(len(payload["sectionDigests"]) == 9 and
               all(item["hashSpecificationVersion"] == V1[1] for item in payload["sectionDigests"]),
               "V1_SECTION_PIN_DRIFT")
    rt.require(SCHEMA not in sent["inputIdentity"] and HASH not in sent["inputIdentity"], "RESULT_PIN_IN_INPUT_IDENTITY")
    revision = rt.row(db, """select id::text,result_schema_version,result_hash_specification_version
        from trading_ledger_revisions where run_id=%s::uuid""", (run_id,))
    rt.require((revision["result_schema_version"], revision["result_hash_specification_version"]) == V1, "REVISION_PIN_DRIFT")
    observations = [a for a in proxy.acks if a["payloadId"] == descriptor["payloadId"]]
    rt.require(observations and all(a["commitVisible"] and a["revisionId"] == revision["id"]
                                   and a["disposition"] == "PERSISTED" for a in observations), "ACK_BEFORE_COMMIT")
    ack_id = rt.row(db, "select id::text from trading_normalization_ack_dispatches where run_id=%s::uuid", (run_id,))["id"]
    passed("V1_PIN_PUBLICATION", runId=run_id, jobId=job_id, revisionId=revision["id"], ackId=ack_id,
           ackPayloadId=descriptor["payloadId"], pair=V1, ackCommitVisible=True)

    begin("TR-ACC-IMPORT-ORCH-024")
    before = stored_job()
    grant_before = rt.row(compute_db, """select jsonb_agg(to_jsonb(g) order by grant_revision,artifact_role) as grants
        from trading_review_grant_revisions g where job_id=%s::uuid""", (job_id,))
    replay = rt.request(compute, "POST", args.compute_url + "/trading-review/normalization-jobs", 202,
                        headers={"Idempotency-Key": dispatch["compute_key"]}, json=sent)
    rt.require(replay == accepted and pair(replay) == V1, "EXACT_REPLAY_DRIFT")
    changed = copy.deepcopy(sent)
    changed.update({SCHEMA: V2[0], HASH: V2[1]})
    response = rt.request(compute, "POST", args.compute_url + "/trading-review/normalization-jobs", 409,
                          headers={"Idempotency-Key": dispatch["compute_key"]}, json=changed)
    rt.require(response["code"] == "IDEMPOTENCY_PAYLOAD_CONFLICT" and stored_job() == before,
               "PAIR_CONFLICT_CHANGED_JOB")
    rt.require(grant_before == rt.row(compute_db, """select jsonb_agg(to_jsonb(g) order by grant_revision,artifact_role) as grants
        from trading_review_grant_revisions g where job_id=%s::uuid""", (job_id,)), "PAIR_CONFLICT_CHANGED_GRANTS")
    passed("TR-ACC-IMPORT-ORCH-024", jobId=job_id, exactReplay=202, otherPair=409, jobAndGrantsUnchanged=True)

    begin("TR-ACC-IMPORT-ORCH-025")
    counts_query = """select (select count(*) from trading_review_jobs) as jobs,
        (select count(*) from trading_review_job_idempotency) as idempotency"""
    count_before = rt.row(compute_db, counts_query, ())
    cases = [("missing_both", None, None, "REQUEST_INVALID"),
             ("missing_schema", None, V1[1], "REQUEST_INVALID"),
             ("missing_hash", V1[0], None, "REQUEST_INVALID"),
             ("cross_v1_v2", V1[0], V2[1], "RESULT_VERSION_PAIR_INVALID"),
             ("cross_v2_v1", V2[0], V1[1], "RESULT_VERSION_PAIR_INVALID"),
             ("unsupported_schema", "TRADING_NORMALIZATION_RESULT_FUTURE", V1[1], "RESULT_SCHEMA_UNSUPPORTED"),
             ("unsupported_hash", V1[0], "TRADING_NORMALIZATION_RESULT_HASH_FUTURE", "RESULT_SCHEMA_UNSUPPORTED"),
             ("unsupported_both", "TRADING_NORMALIZATION_RESULT_FUTURE", "TRADING_NORMALIZATION_RESULT_HASH_FUTURE", "RESULT_SCHEMA_UNSUPPORTED")]
    invalid = []
    for name, schema, spec, code in cases:
        bad = copy.deepcopy(sent)
        for field, value in ((SCHEMA, schema), (HASH, spec)):
            if value is None:
                bad.pop(field)
            else:
                bad[field] = value
        response = rt.request(compute, "POST", args.compute_url + "/trading-review/normalization-jobs", 422,
                              headers={"Idempotency-Key": "pin-invalid-" + uuid.uuid4().hex}, json=bad)
        rt.require(response["code"] == code, "VERSION_CREATE_WRONG_FAILURE")
        rt.require(rt.row(compute_db, counts_query, ()) == count_before, "INVALID_CREATE_PERSISTED")
        invalid.append({"case": name, "status": 422, "code": code})
    passed("TR-ACC-IMPORT-ORCH-025", cases=invalid, jobsAndIdempotencyUnchanged=True)

    for mode in ("descriptor", "payload"):
        begin("TR-ACC-IMPORT-ORCH-026-" + mode)
        fault_session = rt.request(core, "POST", f"{args.core_url}/trading-books/{book['id']}/imports", 201,
            headers={"Idempotency-Key": "pin-session-" + uuid.uuid4().hex},
            json={"sourceTimezone": "Asia/Seoul", "reviewTimezone": "UTC", "declaredPositionMode": "ONE_WAY"})
        sid = fault_session["id"]
        for role, artifact in artifacts.items():
            rt.upload(core, args.core_url, sid, role, args.fixture_root / artifact["path"], artifact["sha256"])
        proxy.mode = (mode, str(sid))
        rt.request(core, "POST", f"{args.core_url}/trading-import-sessions/{sid}/validation", 202,
                   headers={"Idempotency-Key": "pin-validate-" + uuid.uuid4().hex}, json={})
        failed = worker_poll(rt, args, lambda: rt.request(core, "GET", f"{args.core_url}/trading-import-sessions/{sid}", 200),
            lambda s: s["status"] in {"PROCESSING_FAILED", "ACCEPTED", "REJECTED"}, args.timeout, "pin fault " + mode)
        rt.require(failed["status"] == "PROCESSING_FAILED" and
                   failed["failure"]["code"] == "RESULT_VERSION_PIN_MISMATCH" and
                   failed["failure"]["userAction"] == "WAIT_FOR_RECOVERY" and
                   failed["createdLedgerRevisionId"] is None, "PIN_MISMATCH_NOT_BLOCKED")
        fault_job, fault = next((j, f) for j, f in proxy.faults.items() if f["sessionId"] == sid)
        state = rt.row(db, """select n.status,d.status as dispatch_status,
            (select count(*) from trading_ledger_revisions where run_id=n.id) as revisions,
            (select count(*) from trading_normalization_ack_dispatches where run_id=n.id) as acks,
            (select latest_ledger_revision_id::text from trading_books where id=n.trading_book_id) as latest
            from trading_normalization_runs n join trading_normalization_dispatches d on d.run_id=n.id
            where n.id=%s::uuid""", (fault["runId"],))
        rt.require(state == {"status": "TERMINAL_FAILURE", "dispatch_status": "TERMINAL_FAILURE",
                             "revisions": 0, "acks": 0, "latest": revision["id"]}, "PIN_FAULT_PUBLICATION_DRIFT")
        terminal = rt.row(compute_db, """select acknowledged_at from trading_review_terminal_payloads
            where job_id=%s::uuid""", (fault_job,))
        rt.require(terminal and terminal["acknowledged_at"] is None and fault["descriptorReads"] > 0 and
                   (mode != "payload" or fault["payloadReads"] > 0), "PIN_FAULT_NOT_OBSERVED")
        passed("TR-ACC-IMPORT-ORCH-026-" + mode, runId=fault["runId"], jobId=fault_job,
               payloadId=fault["payloadId"], revisions=0, persistedAcks=0, latestRevisionId=revision["id"])
    passed("TR-ACC-IMPORT-ORCH-026", cases=["descriptor", "payload"])

    checks["proxy"] = proxy.evidence()
    rt.require(not proxy.errors, "PIN_PROXY_ERRORS")
    begin("TR-ACC-IMPORT-ORCH-028")
    from trading_normalization_cutover import verify_cutover
    verify_cutover(rt, args, evidence, core, compute, db, compute_db, proxy, book, artifacts)
    passed("TR-ACC-IMPORT-ORCH-028", **checks.pop("cutover"))
    begin("TR-ACC-IMPORT-ORCH-027")
    migration_rehearsal(rt, args, checks, core, compute, db, compute_db, proxy,
                        book, account, session_id, run_id, job_id, sent, dispatch, validation_key)
    passed("TR-ACC-IMPORT-ORCH-027", **checks.pop("migration"))
    evidence["pinProxy"] = proxy.evidence()


def migration_rehearsal(rt, args, checks, core, compute, db, compute_db, proxy,
                        book, account, session_id, run_id, job_id, sent, dispatch, validation_key):
    """Copy ONLY this synthetic publication to pristine pre-pin schemas, then run real SQL.

    No downgrade, DROP COLUMN or trigger bypass occurs in a running service schema.
    The services are subsequently restarted against the migrated copies for HTTP replay.
    """
    suffix = uuid.uuid4().hex[:12]
    core_schema, compute_schema = "tnorm_legacycore" + suffix, "tnorm_legacycompute" + suffix
    checks["migration"] = {"coreSchema": core_schema, "computeSchema": compute_schema,
                           "runId": run_id, "jobId": job_id}
    for name in (core_schema, compute_schema):
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    core_root = args.core_path / "tradingreview/adapter/persistence/src/main/resources/db/migration"
    core_migrations = sorted(core_root.glob("V*__*.sql"), key=lambda p: int(p.name.split("__")[0][1:]))
    compute_migrations = sorted((args.compute_path / "migrations").glob("*.sql"))

    def migrate(connection, paths):
        for path in paths:
            if path.name == "0017_pin_trading_normalization_result_pair.sql":
                # Match initialize-compute-schema.ps1's existing psql -f execution
                # boundary. A single transaction queues the deferred Job/terminal FK
                # across UPDATE + ALTER and is not equivalent to that runner.
                environment = dict(os.environ, PGPASSWORD=os.environ["TRADING_RUNTIME_DB_PASSWORD"],
                                   PGOPTIONS=f"-c search_path={compute_schema}")
                result = subprocess.run([os.environ["TRADING_RUNTIME_PSQL"],
                    "-h", os.environ["TRADING_RUNTIME_DB_HOST"], "-p", os.environ["TRADING_RUNTIME_DB_PORT"],
                    "-U", os.environ["TRADING_RUNTIME_DB_USER"], "-d", "refinvest_integration",
                    "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=sqlstate", "-f", str(path)],
                    env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
                migration_log = args.run_dir / "compute-pin-migration.err.log"
                migration_log.write_bytes(result.stderr)
                checks["migration"]["migrationLogPath"] = str(migration_log)
                rt.require(result.returncode == 0, "COMPUTE_PIN_MIGRATION_FAILED")
                checks["migration"]["computeMigrationRunner"] = "existing psql -f autocommit boundary"
                continue
            with connection.transaction():
                connection.execute(path.read_text(encoding="utf-8"), prepare=False)

    def copy_rows(target, source, table, condition, params=(), changes=None):
        columns = rt.row(target, """select array_agg(column_name::text order by ordinal_position) as names
            from information_schema.columns where table_schema=current_schema() and table_name=%s""", (table,))["names"]
        records = source.execute(sql.SQL("SELECT {} FROM {} WHERE ").format(
            sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier(table)) + sql.SQL(condition), params).fetchall()
        for record in records:
            values = dict(zip(columns, record))
            if changes:
                values.update(changes)
            from psycopg.types.json import Jsonb
            target.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(sql.Identifier(table),
                sql.SQL(",").join(map(sql.Identifier, columns)), sql.SQL(",").join(sql.Placeholder() for _ in columns)),
                [Jsonb(values[c]) if isinstance(values[c], (dict, list)) else values[c] for c in columns])

    with rt.db_connection(core_schema) as legacy_core, rt.db_connection(compute_schema) as legacy_compute:
        migrate(legacy_core, [p for p in core_migrations if int(p.name.split("__")[0][1:]) < 9])
        migrate(legacy_compute, [p for p in compute_migrations if int(p.name[:4]) < 17])
        owner = rt.row(db, "select owner_member_id from trading_books where id=%s", (int(book["id"]),))["owner_member_id"]
        copy_rows(legacy_core, db, "members", "id=%s", (owner,))
        legacy_core.execute(sql.SQL("CREATE TABLE subscriptions (LIKE {}.subscriptions INCLUDING ALL)").format(
            sql.Identifier(os.environ.get("TRADING_RUNTIME_CORE_SCHEMA", "public"))))
        copy_rows(legacy_core, db, "subscriptions", "member_id=%s", (owner,))
        copy_rows(legacy_core, db, "trading_accounts", "id=%s", (int(account["id"]),))
        copy_rows(legacy_core, db, "trading_books", "id=%s", (int(book["id"]),),
                  {"latest_ledger_revision_id": None})
        copy_rows(legacy_core, db, "trading_import_sessions", "id=%s", (int(session_id),),
                  {"created_ledger_revision_id": None})
        copy_rows(legacy_core, db, "trading_source_artifacts", "import_session_id=%s", (int(session_id),))
        saved = rt.row(db, "select * from trading_normalization_runs where id=%s::uuid", (run_id,))
        logical_body = {"ownerScope": str(owner), "inputSetHash": saved["input_fingerprint"],
                        "deletionGeneration": saved["deletion_generation"]}
        old_key = hashlib.sha256(encoded(logical_body)).hexdigest()
        copy_rows(legacy_core, db, "trading_normalization_runs", "id=%s::uuid", (run_id,), {"logical_key": old_key})
        for table in ("trading_normalization_attempts", "trading_normalization_dispatches", "trading_ledger_revisions",
                      "trading_normalization_ack_dispatches"):
            copy_rows(legacy_core, db, table, "run_id=%s::uuid", (run_id,))
        revision = rt.row(db, "select id::text from trading_ledger_revisions where run_id=%s::uuid", (run_id,))["id"]
        legacy_core.execute("UPDATE trading_import_sessions SET created_ledger_revision_id=%s::uuid WHERE id=%s",
                            (revision, int(session_id)))
        legacy_core.execute("UPDATE trading_books SET latest_ledger_revision_id=%s::uuid WHERE id=%s",
                            (revision, int(book["id"])))
        copy_rows(legacy_core, db, "trading_source_evidence_snapshots", "trading_book_id=%s", (int(book["id"]),))
        copy_rows(legacy_core, db, "trading_records", "trading_book_id=%s", (int(book["id"]),))
        for table in ("trading_ledger_revision_records", "trading_ledger_review_units", "trading_ledger_allocations"):
            copy_rows(legacy_core, db, table, "revision_id=%s::uuid", (revision,))
        copy_rows(legacy_core, db, "trading_idempotency", "owner_member_id=%s AND request_key=%s", (owner, validation_key))
        pre_request = copy.deepcopy(sent)
        pre_request.pop(SCHEMA)
        pre_request.pop(HASH)
        # Persistence strips capability locators, as the original pre-pin producer did.
        for grant in pre_request["artifactAccessGrants"]:
            grant.pop("accessUri", None)
        pre_fp = fingerprint(pre_request)
        # The production Job -> terminal FK is deferred across the creation transaction.
        with legacy_compute.transaction():
            copy_rows(legacy_compute, compute_db, "trading_review_jobs", "job_id=%s::uuid", (job_id,),
                      {"request_payload": pre_request, "request_fingerprint": pre_fp})
            copy_rows(legacy_compute, compute_db, "trading_review_job_idempotency", "job_id=%s::uuid", (job_id,),
                      {"request_fingerprint": pre_fp})
            for table in ("trading_review_job_attempts", "trading_review_terminal_payloads", "trading_review_grant_revisions",
                          "trading_review_job_acknowledgements", "trading_review_attempt_tokens"):
                copy_rows(legacy_compute, compute_db, table, "job_id=%s::uuid", (job_id,))
        revision_before = rt.row(legacy_core, "select * from trading_ledger_revisions where run_id=%s::uuid", (run_id,))
        job_before = rt.row(legacy_compute, "select * from trading_review_jobs where job_id=%s::uuid", (job_id,))
        ack_before = rt.row(legacy_core, "select * from trading_normalization_ack_dispatches where run_id=%s::uuid", (run_id,))
        # Diagnose the separate atomic-runner limitation against a real terminal Job.
        # Always roll this attempt back, then use the existing statement runner below.
        legacy_compute.execute("BEGIN")
        try:
            legacy_compute.execute((args.compute_path / "migrations/0017_pin_trading_normalization_result_pair.sql")
                .read_text(encoding="utf-8"), prepare=False)
            checks["migration"]["atomicRunner"] = {"result": "PASS", "rolledBackDiagnostic": True}
        except psycopg.Error as error:
            rt.require(error.sqlstate == "55006", "UNEXPECTED_ATOMIC_MIGRATION_FAILURE")
            checks["migration"]["atomicRunner"] = {"result": "FAIL", "sqlstate": error.sqlstate,
                "knownDeploymentLimitation": True, "rolledBackDiagnostic": True}
        finally:
            legacy_compute.execute("ROLLBACK")
        migrate(legacy_core, [p for p in core_migrations if int(p.name.split("__")[0][1:]) >= 9])
        migrate(legacy_compute, [p for p in compute_migrations if int(p.name[:4]) == 17])
        for table, conn, field, identifier in (
            ("trading_normalization_runs", legacy_core, "id", run_id),
            ("trading_normalization_dispatches", legacy_core, "run_id", run_id),
            ("trading_review_jobs", legacy_compute, "job_id", job_id)):
            value = conn.execute(sql.SQL("select result_schema_version,result_hash_specification_version from {} where {}=%s::uuid")
                                 .format(sql.Identifier(table), sql.Identifier(field)), (identifier,)).fetchone()
            rt.require(value == V1, "MIGRATION_PIN_DRIFT")
        logical_body.update({SCHEMA: V1[0], HASH: V1[1]})
        migrated_run = rt.row(legacy_core, "select * from trading_normalization_runs where id=%s::uuid", (run_id,))
        rt.require(migrated_run == saved and
                   migrated_run["logical_key"] == hashlib.sha256(encoded(logical_body)).hexdigest() and
                   migrated_run["status"] == saved["status"] and
                   rt.row(legacy_core, "select compute_key from trading_normalization_dispatches where run_id=%s::uuid", (run_id,))["compute_key"] == dispatch["compute_key"], "MIGRATION_RUN_IDEMPOTENCY_DRIFT")
        migrated_job = rt.row(legacy_compute, "select * from trading_review_jobs where job_id=%s::uuid", (job_id,))
        for key, value in job_before.items():
            if key not in {"request_payload", "request_fingerprint"}:
                rt.require(migrated_job[key] == value, "MIGRATION_JOB_DRIFT")
        rt.require(migrated_job["request_fingerprint"] == fingerprint(sent) and pair(migrated_job["request_payload"]) == V1,
                   "MIGRATION_FINGERPRINT_DRIFT")
        migrated_idempotency = rt.row(legacy_compute, """select request_fingerprint,job_id::text from
            trading_review_job_idempotency where job_id=%s::uuid""", (job_id,))
        expected_request = copy.deepcopy(pre_request)
        expected_request.update({SCHEMA: V1[0], HASH: V1[1]})
        rt.require(migrated_idempotency == {"request_fingerprint": fingerprint(sent), "job_id": job_id} and
                   migrated_job["request_payload"] == expected_request, "MIGRATION_REQUEST_OR_IDEMPOTENCY_DRIFT")
        rt.require(rt.row(legacy_core, "select * from trading_ledger_revisions where run_id=%s::uuid", (run_id,)) == revision_before and
                   rt.row(legacy_core, "select * from trading_normalization_ack_dispatches where run_id=%s::uuid", (run_id,)) == ack_before,
                   "MIGRATION_CHANGED_REVISION_OR_ACK")

        import shutil
        log_archive = args.run_dir / "before-migration-logs"
        log_archive.mkdir()
        for log in (args.test_root / ".integration-data/logs").glob(args.registry + "-*.log"):
            shutil.copy2(log, log_archive / log.name)
        checks["migration"]["beforeRestartLogDirectory"] = str(log_archive)
        scripts = args.test_root / "scripts"
        for service in ("core-api", "compute-worker", "compute-api"):
            rt.process_script(scripts / "stop-integration.ps1", "-RegistryId", args.registry, "-Service", service)
        proxy.core_schema = core_schema
        migrated_evidence = args.run_dir / "migration-startup"
        migrated_evidence.mkdir()
        rt.process_script(scripts / "start-trading-normalization-service.ps1", "-EnvironmentFile", str(args.environment_file),
                          "-Service", "compute-api", "-ComputePath", str(args.compute_path), "-DatabaseSchema", compute_schema,
                          "-EvidenceDirectory", str(migrated_evidence), "-RegistryId", args.registry)
        rt.process_script(scripts / "start-trading-normalization-service.ps1", "-EnvironmentFile", str(args.environment_file),
                          "-CorePath", str(args.core_path), "-DatabaseSchema", core_schema,
                          "-ComputeBaseUrl", f"http://127.0.0.1:{args.fixture_port}",
                          "-EvidenceDirectory", str(migrated_evidence), "-RegistryId", args.registry)
        rt.poll(lambda: rt.health_code(compute, args.compute_url + "/health"), lambda c: c == 200, args.timeout, "migrated Compute startup")
        rt.poll(lambda: rt.health_code(core, args.core_url + "/actuator/health"), lambda c: c == 200, 600, "migrated Core startup")
        migrated_status = rt.request(compute, "GET", f"{args.compute_url}/trading-review/normalization-jobs/{job_id}", 200)
        rt.require(migrated_status["status"] == "COMPLETED" and pair(migrated_status) == V1 and
                   migrated_status["inputSetHash"] == saved["input_fingerprint"], "MIGRATED_STATUS_DRIFT")
        migrated_descriptor = rt.request(compute, "GET",
            f"{args.compute_url}/trading-review/normalization-jobs/{job_id}/result", 200)
        migrated_payload = compute.get(args.compute_url + "/trading-review/terminal-payloads/" + migrated_descriptor["payloadId"])
        rt.require(migrated_payload.status_code == 200 and pair(migrated_descriptor) == V1 and
                   pair(migrated_payload.json()) == V1 and
                   migrated_descriptor["resultHash"] == revision_before["result_hash"] and
                   hashlib.sha256(migrated_payload.content).hexdigest() == migrated_descriptor["payloadSha256"],
                   "MIGRATED_TERMINAL_DRIFT")
        replay = rt.request(compute, "POST", args.compute_url + "/trading-review/normalization-jobs", 202,
                            headers={"Idempotency-Key": dispatch["compute_key"]}, json=sent)
        rt.require(replay["jobId"] == job_id and pair(replay) == V1, "MIGRATED_COMPUTE_REPLAY_DRIFT")
        rt.request(core, "POST", f"{args.core_url}/trading-import-sessions/{session_id}/validation", 202,
                   headers={"Idempotency-Key": validation_key}, json={})
        public = rt.request(core, "GET", f"{args.core_url}/trading-import-sessions/{session_id}", 200)
        rt.require(public["status"] == "ACCEPTED" and public["createdLedgerRevisionId"] == revision, "MIGRATED_CORE_REPLAY_DRIFT")
        invalid = rt.request(compute, "POST", args.compute_url + "/trading-review/normalization-jobs", 422,
                             headers={"Idempotency-Key": "post-migration-" + uuid.uuid4().hex}, json=pre_request)
        rt.require(invalid["code"] == "REQUEST_INVALID", "MIGRATION_ENABLED_IMPLICIT_DEFAULT")
        rt.require(rt.row(legacy_core, "select * from trading_ledger_revisions where run_id=%s::uuid", (run_id,)) == revision_before and
                   rt.row(legacy_core, "select * from trading_normalization_ack_dispatches where run_id=%s::uuid", (run_id,)) == ack_before,
                   "RESTART_CHANGED_REVISION_OR_ACK")
        rt.require(rt.row(legacy_core, "select count(*) as total from trading_normalization_runs", ())["total"] == 1 and
                   rt.row(legacy_compute, "select count(*) as total from trading_review_jobs", ())["total"] == 1,
                   "MIGRATION_REPLAY_DUPLICATED")
        checks["migration"].update(revisionId=revision, coreMigration="V9", computeMigration="0017",
                                   serviceRestart=True, explicitReplay=True, revisionAndAckUnchanged=True)
