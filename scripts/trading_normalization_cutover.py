"""ORCH-028 against real Core instances, Compute HTTP and integration PostgreSQL.

Only synthetic-session transport faults and scheduling delays are injected. No
application implementation or result is replaced by a local business-code copy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
from psycopg import sql

from trading_normalization_version_pin import PinProxy, V1, pair


ITEMS = (
    "01-validation-and-retry-503", "02-rejected-state-unchanged", "03-zero-create-while-paused",
    "04-two-core-instances", "05-admission-and-send-races", "06-restart-and-unsent-preserved",
    "07-terminal-deletion-cancellation-recovery", "08-failed-http-keeps-paused",
    "09-verified-resume-same-public-keys", "10-no-duplicate-run-job-revision-ack",
)


def service(rt, args, script, *arguments, environment=None):
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
        str(args.test_root / "scripts" / script), *map(str, arguments)], env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    rt.require(result.returncode == 0, "CUTOVER_PROCESS_CONTROL_FAILED")


def environment_file(args, name, core_port, compute_port):
    destination = args.run_dir / (name + ".env")
    overrides = {"INTEGRATION_CORE_PORT": str(core_port), "INTEGRATION_COMPUTE_PORT": str(compute_port)}
    lines = []
    for line in args.environment_file.read_text(encoding="utf-8-sig").splitlines():
        key = line.split("=", 1)[0].strip()
        lines.append(key + "=" + overrides.pop(key) if key in overrides else line)
    lines.extend(k + "=" + v for k, v in overrides.items())
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def new_session(rt, args, core, book, artifacts):
    # Re-importing identical source into a published Book exercises overlap/conflict
    # semantics. Each independent cutover fixture instead owns a fresh Book.
    book = rt.request(core, "POST", f"{args.core_url}/trading-accounts/{book['tradingAccountId']}/books", 201,
        headers={"Idempotency-Key": "cutover-book-" + uuid.uuid4().hex},
        json={"displayName": "Synthetic cutover", "productFamily": "LINEAR_PERPETUAL",
            "positionMode": "ONE_WAY", "settlementAsset": "USDT", "reviewTimezone": "UTC"})
    session = rt.request(core, "POST", f"{args.core_url}/trading-books/{book['id']}/imports", 201,
        headers={"Idempotency-Key": "cutover-import-" + uuid.uuid4().hex},
        json={"sourceTimezone": "Asia/Seoul", "reviewTimezone": "UTC", "declaredPositionMode": "ONE_WAY"})
    uploaded = {}
    for role, artifact in artifacts.items():
        uploaded[role] = rt.upload(core, args.core_url, session["id"], role,
            args.fixture_root / artifact["path"], artifact["sha256"])
    return session["id"], uploaded


def validate(rt, args, core, sid, key=None, endpoint="validation", expected=202):
    return rt.request(core, "POST", f"{args.core_url}/trading-import-sessions/{sid}/{endpoint}", expected,
        headers={"Idempotency-Key": key or "cutover-validation-" + uuid.uuid4().hex}, json={})


def state(rt, args, core, sid):
    return rt.request(core, "GET", f"{args.core_url}/trading-import-sessions/{sid}", 200)


def dispatch(rt, db, sid):
    return rt.row(db, """select d.*,n.compute_job_id,n.attempt_token::text,n.input_fingerprint,
        n.deletion_generation,n.target_version_set_json,n.import_session_id from
        trading_normalization_dispatches d join trading_normalization_runs n on n.id=d.run_id
        where n.import_session_id=%s order by d.created_at desc limit 1""", (int(sid),))


def control(rt, db, command, epoch, reference):
    return rt.row(db, "select trading_normalization_transition_control(%s,%s,%s) as epoch",
        (command, epoch, reference))["epoch"]


def current(rt, db):
    return rt.row(db, "select paused,epoch,verified_evidence from trading_normalization_transition where singleton", ())


def snapshot(rt, db, sid, keys):
    return rt.row(db, """select
        (select to_jsonb(s) from trading_import_sessions s where id=%s) as session,
        (select jsonb_agg(to_jsonb(n) order by id) from trading_normalization_runs n where import_session_id=%s) as runs,
        (select jsonb_agg(to_jsonb(d) order by d.id) from trading_normalization_dispatches d
            join trading_normalization_runs n on n.id=d.run_id where n.import_session_id=%s) as dispatches,
        (select jsonb_agg(to_jsonb(i) order by i.request_key) from trading_idempotency i where request_key=any(%s)) as idempotency,
        (select count(*) from trading_import_sessions) as session_count,
        (select count(*) from trading_normalization_runs) as run_count,
        (select count(*) from trading_normalization_dispatches) as dispatch_count,
        (select count(*) from trading_idempotency) as idempotency_count""", (int(sid), int(sid), int(sid), keys))


def expect_control_rejected(rt, db, command, epoch, reference):
    try:
        control(rt, db, command, epoch, reference)
    except psycopg.Error:
        rt.require(current(rt, db)["paused"], "FAILED_VERIFICATION_OPENED_GATE")
    else:
        raise rt.ScenarioFailure("UNVERIFIED_CONTROL_ACCEPTED")


def isolated_canary(rt, args, core, db, artifacts, checks):
    """Run the candidate pair in separate schemas while the original fleet stays paused."""
    suffix = uuid.uuid4().hex[:12]
    core_schema, compute_schema = "tnorm_canarycore" + suffix, "tnorm_canarycompute" + suffix
    core_port = int(args.core_url.rsplit(":", 1)[1]) + 20
    compute_port = int(args.compute_url.rsplit(":", 1)[1]) + 20
    canary_args = SimpleNamespace(**vars(args))
    canary_args.core_url, canary_args.compute_url = f"http://127.0.0.1:{core_port}", f"http://127.0.0.1:{compute_port}"
    canary_args.fixture_port = compute_port + 1
    canary_args.compute_schema = compute_schema
    registry = args.registry + "-canary"
    env_file = environment_file(args, "canary", core_port, compute_port)
    owner = rt.row(db, "select owner_member_id from trading_books order by id desc limit 1", ())["owner_member_id"]
    for name in (core_schema, compute_schema):
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    with rt.db_connection(core_schema) as canary_db, rt.db_connection(compute_schema) as canary_compute_db:
        for table, condition in (("members", "id"), ("subscriptions", "member_id")):
            source_schema = os.environ.get("TRADING_RUNTIME_CORE_SCHEMA", "public")
            canary_db.execute(sql.SQL("CREATE TABLE {} (LIKE {}.{} INCLUDING ALL)").format(
                sql.Identifier(table), sql.Identifier(source_schema), sql.Identifier(table)))
            canary_db.execute(sql.SQL("INSERT INTO {} SELECT * FROM {}.{} WHERE {}=%s").format(
                sql.Identifier(table), sql.Identifier(source_schema), sql.Identifier(table), sql.Identifier(condition)), (owner,))
        migration_root = args.core_path / "tradingreview/adapter/persistence/src/main/resources/db/migration"
        for path in sorted(migration_root.glob("V*__*.sql"), key=lambda p: int(p.name.split("__")[0][1:])):
            with canary_db.transaction():
                canary_db.execute(path.read_text(encoding="utf-8"), prepare=False)
        for path in sorted((args.compute_path / "migrations").glob("*.sql")):
            with canary_compute_db.transaction():
                canary_compute_db.execute(path.read_text(encoding="utf-8"), prepare=False)
        environment = dict(os.environ)
        config = json.loads(environment["SPRING_APPLICATION_JSON"])
        config.update({"refinvest.trading-review.grant-base-url": canary_args.core_url,
                       "refinvest.id.node-id": 2, "refinvest.compute.result-delay": "PT1S",
                       "refinvest.compute.ack-delay": "PT1S"})
        environment["SPRING_APPLICATION_JSON"] = json.dumps(config)
        environment["REFINVEST_TRADING_REVIEW_ARTIFACT_HOSTS"] += f",127.0.0.1:{core_port}"
        canary_proxy = PinProxy(rt, canary_args)
        canary_proxy.core_schema, canary_proxy.reject_acks = core_schema, False
        try:
            service(rt, args, "start-trading-normalization-service.ps1", "-EnvironmentFile", env_file,
                "-Service", "compute-api", "-ComputePath", args.compute_path, "-DatabaseSchema", compute_schema,
                "-RegistryId", registry, "-EvidenceDirectory", args.run_dir, environment=environment)
            service(rt, args, "start-trading-normalization-service.ps1", "-EnvironmentFile", env_file,
                "-CorePath", args.core_path, "-DatabaseSchema", core_schema, "-RegistryId", registry,
                "-EvidenceDirectory", args.run_dir, "-ComputeBaseUrl", f"http://127.0.0.1:{canary_args.fixture_port}", environment=environment)
            service(rt, args, "start-compute-worker.ps1", "-EnvironmentFile", env_file,
                "-ComputeApiPath", args.compute_path, "-Schema", compute_schema,
                "-RegistryId", registry, "-Profile", registry, "-TradingReview", environment=environment)
            account = rt.request(core, "POST", canary_args.core_url + "/trading-accounts", 201,
                headers={"Idempotency-Key": "canary-account-" + suffix}, json={"venue": "BINANCE", "displayName": "Synthetic canary"})
            book = rt.request(core, "POST", canary_args.core_url + f"/trading-accounts/{account['id']}/books", 201,
                headers={"Idempotency-Key": "canary-book-" + suffix}, json={"displayName": "Synthetic canary",
                    "productFamily": "LINEAR_PERPETUAL", "positionMode": "ONE_WAY", "settlementAsset": "USDT", "reviewTimezone": "UTC"})
            # The negative canary is a real accepted V1 Job with a scoped transport fault.
            sid, _ = new_session(rt, canary_args, core, book, artifacts)
            canary_proxy.mode = ("descriptor", str(sid))
            validate(rt, canary_args, core, sid)
            failed = rt.poll(lambda: state(rt, canary_args, core, sid),
                lambda s: s["status"] in {"PROCESSING_FAILED", "ACCEPTED"}, args.timeout, "negative canary")
            rt.require(failed["status"] == "PROCESSING_FAILED" and failed["failure"]["code"] == "RESULT_VERSION_PIN_MISMATCH",
                "NEGATIVE_CANARY_NOT_REJECTED")
            rt.require(current(rt, db)["paused"] and current(rt, db)["verified_evidence"] is None, "FAILED_CANARY_RESUMED")
            expect_control_rejected(rt, db, "RESUME", current(rt, db)["epoch"], "failed-canary")
            checks[ITEMS[7]] = {"result": "PASS", "httpFault": "RESULT_VERSION_PIN_MISMATCH", "originalFleetPaused": True}
            sid, uploaded = new_session(rt, canary_args, core, book, artifacts)
            key = "canary-validation-" + suffix
            validate(rt, canary_args, core, sid, key)
            item = rt.poll(lambda: dispatch(rt, canary_db, sid), lambda d: d and d["compute_job_id"], args.timeout, "canary create")
            job_id, run_id = item["compute_job_id"], str(item["run_id"])
            sent, accepted = canary_proxy.creates[run_id]
            rt.require(pair(sent) == pair(accepted) == V1, "CANARY_CREATE_PAIR_DRIFT")
            with httpx.Client(timeout=20, headers={"X-Internal-Api-Key": os.environ["TRADING_RUNTIME_API_KEY"]}) as compute:
                terminal = rt.poll(lambda: rt.request(compute, "GET", canary_args.compute_url + f"/trading-review/normalization-jobs/{job_id}", 200),
                    lambda s: s["status"] in {"COMPLETED", "FAILED"}, args.timeout, "canary terminal")
                rt.require(terminal["status"] == "COMPLETED" and pair(terminal) == V1, "CANARY_STATUS_FAILED")
                descriptor = rt.request(compute, "GET", canary_args.compute_url + f"/trading-review/normalization-jobs/{job_id}/result", 200)
                response = compute.get(canary_args.compute_url + "/trading-review/terminal-payloads/" + descriptor["payloadId"])
                payload = rt.verify_transport(response.content, descriptor, response.headers.get("Content-Type", ""), response.headers.get("Content-Encoding", ""))
                rt.verify(payload, descriptor, {r: a["id"] for r, a in uploaded.items()}, {r: a["sha256"] for r, a in artifacts.items()})
                replay = rt.request(compute, "POST", canary_args.compute_url + "/trading-review/normalization-jobs", 202,
                    headers={"Idempotency-Key": item["compute_key"]}, json=sent)
                rt.require(replay == accepted and pair(descriptor) == pair(payload) == V1, "CANARY_REPLAY_DRIFT")
            published = rt.poll(lambda: state(rt, canary_args, core, sid), lambda s: s["status"] == "ACCEPTED", args.timeout, "canary publication")
            rt.poll(lambda: rt.row(canary_db, "select status from trading_normalization_ack_dispatches where run_id=%s::uuid", (run_id,)),
                lambda a: a and a["status"] == "ACKNOWLEDGED", args.timeout, "canary ACK")
            validate(rt, canary_args, core, sid, key)
            rt.require(not canary_proxy.errors, "CANARY_PROXY_ERRORS")
            return {"coreSchema": core_schema, "computeSchema": compute_schema, "runId": run_id, "jobId": job_id,
                "revisionId": published["createdLedgerRevisionId"], "createAcceptedStatusTerminalReplay": "PASS",
                "independentV1Integrity": "PASS", "commits": {"context": rt.git_revision(args.context_path),
                    "core": rt.git_revision(args.core_path), "compute": rt.git_revision(args.compute_path)}}
        finally:
            for role in ("core-api", "compute-worker", "compute-api"):
                service(rt, args, "stop-integration.ps1", "-RegistryId", registry, "-Service", role)
            canary_proxy.close()


def verify_cutover(rt, args, evidence, core, compute, db, compute_db, proxy, book, artifacts):
    checks = {name: {"result": "NOT_RUN"} for name in ITEMS}
    result = evidence["versionPin"]["cutover"] = {"assertions": checks, "result": "RUNNING"}
    def checkpoint():
        (args.run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8")
    checkpoint()
    start = current(rt, db)
    rt.require(not start["paused"], "CUTOVER_ALREADY_PAUSED_REQUIRES_RECOVERY")
    secondary_registry = args.registry + "-second"
    secondary_port = int(args.core_url.rsplit(":", 1)[1]) + 10
    secondary_args = SimpleNamespace(**vars(args))
    secondary_args.core_url = f"http://127.0.0.1:{secondary_port}"
    secondary_env_file = environment_file(args, "second-core", secondary_port, int(args.compute_url.rsplit(":", 1)[1]))
    secondary_environment = dict(os.environ)
    config = json.loads(secondary_environment["SPRING_APPLICATION_JSON"])
    config["refinvest.id.node-id"] = 1
    secondary_environment["SPRING_APPLICATION_JSON"] = json.dumps(config)
    executor = ThreadPoolExecutor(max_workers=3)
    admission_lock = None
    try:
        # Make a genuinely retryable Session through the actual dispatcher retry budget.
        retry_sid, _ = new_session(rt, args, core, book, artifacts)
        proxy.reject_create_sessions.add(str(retry_sid))
        validate(rt, args, core, retry_sid)
        failed = rt.poll(lambda: state(rt, args, core, retry_sid),
            lambda s: s["status"] == "PROCESSING_FAILED", args.timeout, "retryable Session")
        rt.require(failed["failure"]["retryable"], "SESSION_NOT_RETRYABLE")
        proxy.reject_create_sessions.remove(str(retry_sid))
        ack_sid, _ = new_session(rt, args, core, book, artifacts)
        proxy.reject_acks = True
        validate(rt, args, core, ack_sid)
        ack_public = rt.poll(lambda: state(rt, args, core, ack_sid),
            lambda s: s["status"] in {"ACCEPTED", "PROCESSING_FAILED", "REJECTED"}, args.timeout, "existing ACK publication")
        rt.require(ack_public["status"] == "ACCEPTED", "EXISTING_ACK_PUBLICATION_FAILED")
        ack_run = str(dispatch(rt, db, ack_sid)["run_id"])
        rt.poll(lambda: rt.row(db, "select status,attempt_count from trading_normalization_ack_dispatches where run_id=%s::uuid", (ack_run,)),
            lambda a: a and a["status"] == "PENDING" and a["attempt_count"] > 0, args.timeout, "existing ACK failure")
        # Keep the real, already-failed ACK pending across process startup without
        # exhausting its immutable retry budget. Only the synthetic due time changes.
        db.execute("""update trading_normalization_ack_dispatches set next_attempt_at=clock_timestamp()+interval '20 minutes'
            where run_id=%s::uuid and status='PENDING'""", (ack_run,))
        # Leave queued Jobs pending to test actual cancellation and terminal drain after PAUSE.
        service(rt, args, "stop-integration.ps1", "-RegistryId", args.registry, "-Service", "compute-worker")
        service(rt, args, "start-trading-normalization-service.ps1", "-EnvironmentFile", secondary_env_file,
            "-CorePath", args.core_path, "-RegistryId", secondary_registry, "-EvidenceDirectory", args.run_dir,
            "-DatabaseSchema", os.environ.get("TRADING_RUNTIME_CORE_SCHEMA", "public"),
            "-ComputeBaseUrl", f"http://127.0.0.1:{args.fixture_port}", environment=secondary_environment)
        received_sid, _ = new_session(rt, args, core, book, artifacts)
        unsent_sid, _ = new_session(rt, args, core, book, artifacts)
        validate(rt, args, core, unsent_sid)
        # Test-only scheduling delay, confined to this synthetic dispatch. No identity is edited.
        db.execute("""update trading_normalization_dispatches set next_attempt_at=clock_timestamp()+interval '20 minutes'
            where run_id=(select active_normalization_run_id from trading_import_sessions where id=%s)
            and create_started_at is null and status='PENDING'""", (int(unsent_sid),))
        unsent_before = dispatch(rt, db, unsent_sid)
        rt.require(unsent_before["attempt_count"] == 0 and unsent_before["create_started_at"] is None, "UNSENT_FIXTURE_ALREADY_CLAIMED")
        unknown_sid, _ = new_session(rt, args, core, book, artifacts)
        proxy.drop_response_sessions.add(str(unknown_sid))
        validate(rt, args, core, unknown_sid)
        unknown = rt.poll(lambda: dispatch(rt, db, unknown_sid),
            lambda d: d and d["last_failure_code"] == "CREATE_OUTCOME_UNKNOWN", args.timeout, "ambiguous create")
        rt.require(unknown["compute_job_id"] is None, "UNKNOWN_CREATE_NOT_HELD")
        cancel_sid, _ = new_session(rt, args, core, book, artifacts)
        validate(rt, args, core, cancel_sid)
        cancel = rt.poll(lambda: dispatch(rt, db, cancel_sid), lambda d: d and d["compute_job_id"], args.timeout, "cancellable Job")
        send_sid, _ = new_session(rt, args, core, book, artifacts)
        admission_sid, _ = new_session(rt, args, core, book, artifacts)
        proxy.block_create_session = str(send_sid)
        validate(rt, args, core, send_sid)
        rt.require(proxy.block_entered.wait(15), "SEND_RACE_NOT_ENTERED")
        admission_lock = rt.db_connection("public")
        admission_lock.autocommit = False
        admission_lock.execute("select id from trading_import_sessions where id=%s for update", (int(admission_sid),))
        admitted = executor.submit(validate, rt, secondary_args, core, admission_sid)
        rt.poll(lambda: rt.row(db, "select count(*) as n from pg_stat_activity where datname=current_database() and wait_event_type='Lock'", ()),
            lambda s: s["n"] >= 1, 5, "admission row lock")
        def pause():
            with rt.db_connection("public") as operator:
                return control(rt, operator, "PAUSE", start["epoch"], "orch028-cutover")
        stopping = executor.submit(pause)
        rt.poll(lambda: rt.row(db, "select count(*) as n from pg_stat_activity where datname=current_database() and wait_event='advisory'", ()),
            lambda s: s["n"] >= 1, 5, "stop waits for send and admission")
        rt.require(not stopping.done(), "PAUSE_OVERRAN_INFLIGHT_PERMITS")
        admission_lock.commit()
        admission_lock.close()
        admission_lock = None
        proxy.block_release.set()
        admitted.result(timeout=15)
        epoch = stopping.result(timeout=30)
        pause_at = time.monotonic()
        rt.require(current(rt, db)["paused"], "STOP_NOT_DURABLE")
        proxy.reject_acks = False
        db.execute("update trading_normalization_ack_dispatches set next_attempt_at=clock_timestamp() where run_id=%s::uuid and status='PENDING'", (ack_run,))
        rt.poll(lambda: rt.row(db, "select status from trading_normalization_ack_dispatches where run_id=%s::uuid", (ack_run,)),
            lambda a: a and a["status"] == "ACKNOWLEDGED", args.timeout, "existing ACK retry while paused")
        checks[ITEMS[4]] = {"result": "PASS", "pauseWaitedForClaimSendReceiptAndAdmission": True}
        checkpoint()
        validation_key, retry_key = "paused-validation-" + uuid.uuid4().hex, "paused-retry-" + uuid.uuid4().hex
        before = {sid: snapshot(rt, db, sid, [validation_key, retry_key]) for sid in (received_sid, retry_sid)}
        for base in (args, secondary_args):
            for sid, endpoint, key in ((received_sid, "validation", validation_key), (retry_sid, "retry", retry_key)):
                failure = validate(rt, base, core, sid, key, endpoint, 503)
                rt.require(failure["code"] == "TEMPORARY_DEPENDENCY_FAILURE" and failure["retryable"] is True and
                    bool(failure["message"]) and bool(failure["traceId"]), "PAUSED_FAILURE_CONTRACT_DRIFT")
                rt.require(snapshot(rt, db, sid, [validation_key, retry_key]) == before[sid], "REJECTED_REQUEST_CHANGED_STATE")
        checks[ITEMS[0]], checks[ITEMS[1]], checks[ITEMS[3]] = ({"result": "PASS"}, {"result": "PASS", "rowCountsAndValuesUnchanged": True},
            {"result": "PASS", "coreInstances": 2, "nodeIds": [0, 1]})
        checkpoint()
        expect_control_rejected(rt, db, "VERIFY", epoch, "unresolved-create")
        # Match real HTTP status and the existing Compute idempotency row before attaching the Job.
        sent, accepted = proxy.creates[str(unknown["run_id"])]
        job_id = accepted["jobId"]
        status = rt.request(compute, "GET", args.compute_url + f"/trading-review/normalization-jobs/{job_id}", 200)
        identity = rt.row(compute_db, "select job_id::text from trading_review_job_idempotency where idempotency_key_hash=%s",
            (hashlib.sha256(unknown["compute_key"].encode()).hexdigest(),))
        rt.require(identity and identity["job_id"] == job_id and status["coreLogicalJobId"] == str(unknown["id"]) and
            status["coreNormalizationRunId"] == str(unknown["run_id"]) and status["attemptToken"] == unknown["attempt_token"] and
            status["deletionGeneration"] == unknown["deletion_generation"] and status["expectedInputSetHash"] == unknown["input_fingerprint"] and
            pair(status) == V1 and status["targetVersionSet"] == unknown["target_version_set_json"], "UNKNOWN_CREATE_IDENTITY_DRIFT")
        db.execute("select trading_normalization_reconcile_create(%s,%s,%s,%s)",
            (unknown["id"], job_id, epoch, "orch028-http-idempotency-match"))
        rt.require(sum(a["sessionId"] == str(unknown_sid) for a in proxy.create_attempts) == 1, "UNKNOWN_CREATE_RETRANSMITTED")
        cancellation = rt.request(compute, "POST", args.compute_url + f"/trading-review/normalization-jobs/{cancel['compute_job_id']}/cancellation", 202,
            headers={"Idempotency-Key": "cutover-cancel-" + uuid.uuid4().hex})
        rt.require(cancellation["status"] == "CANCELLED", "PAUSED_CANCELLATION_FAILED")
        rt.request(core, "DELETE", args.core_url + f"/trading-import-sessions/{admission_sid}", 202,
            headers={"Idempotency-Key": "cutover-delete-" + uuid.uuid4().hex})
        rt.require(core.get(args.core_url + f"/trading-import-sessions/{admission_sid}").status_code == 410, "PAUSED_DELETION_ACCESSIBLE")
        service(rt, args, "start-compute-worker.ps1", "-EnvironmentFile", args.environment_file,
            "-ComputeApiPath", args.compute_path, "-Schema", args.compute_schema, "-RegistryId", args.registry,
            "-Profile", args.registry, "-TradingReview")
        rt.poll(lambda: dispatch(rt, db, send_sid), lambda d: d and d["status"] == "RESULT_READY", args.timeout, "paused terminal polling")
        rt.poll(lambda: state(rt, args, core, cancel_sid), lambda s: s["status"] == "PROCESSING_FAILED", args.timeout, "paused cancellation terminal")
        for sid in (send_sid, unknown_sid):
            rt.require(rt.row(db, """select count(*) as n from trading_normalization_ack_dispatches where
                core_import_session_id=%s and disposition='PERSISTED'""", (int(sid),))["n"] == 0, "NEW_PERSISTED_DURING_STOP")
        checks[ITEMS[6]] = {"result": "PASS", "terminalPolling": True, "cancellation": True,
            "deletion": True, "sameJobReconciled": job_id, "existingAckRetry": True, "newPersistedAcks": 0}
        checkpoint()
        service(rt, args, "stop-integration.ps1", "-RegistryId", args.registry, "-Service", "core-api")
        service(rt, args, "start-trading-normalization-service.ps1", "-EnvironmentFile", args.environment_file,
            "-CorePath", args.core_path, "-RegistryId", args.registry, "-EvidenceDirectory", args.run_dir,
            "-DatabaseSchema", os.environ.get("TRADING_RUNTIME_CORE_SCHEMA", "public"),
            "-ComputeBaseUrl", f"http://127.0.0.1:{args.fixture_port}")
        validate(rt, args, core, received_sid, validation_key, expected=503)
        rt.require(dispatch(rt, db, unsent_sid) == unsent_before and current(rt, db)["paused"], "RESTART_LOST_STOP_OR_DISPATCH")
        checks[ITEMS[5]] = {"result": "PASS", "unsentDispatchId": str(unsent_before["id"]), "epoch": epoch}
        checkpoint()
        # Stop the second instance before starting a separate canary fleet.
        service(rt, args, "stop-integration.ps1", "-RegistryId", secondary_registry, "-Service", "core-api")
        canary = isolated_canary(rt, args, core, db, artifacts, checks)
        rt.poll(lambda: dispatch(rt, db, send_sid),
            lambda d: d and d["last_failure_code"] == "NORMALIZATION_TRANSITION_PAUSED", args.timeout, "publication paused after staging")
        rt.require(rt.row(db, """select (select count(*) from trading_ledger_revisions where import_session_id=%s) as revisions,
            (select count(*) from trading_normalization_ack_dispatches where core_import_session_id=%s) as acks""",
            (int(send_sid), int(send_sid))) == {"revisions": 0, "acks": 0}, "NEW_PUBLICATION_DURING_STOP")
        rt.require(not [a for a in proxy.create_attempts if a["at"] > pause_at], "CREATE_SENT_WHILE_PAUSED")
        checks[ITEMS[2]] = {"result": "PASS", "newCreateHttpRequests": 0, "canarySchemasExcluded": True}
        reference = "sha256/" + hashlib.sha256(json.dumps(canary, sort_keys=True).encode()).hexdigest()
        (args.run_dir / "canary-verification.json").write_text(json.dumps(canary, sort_keys=True), encoding="utf-8")
        control(rt, db, "VERIFY", epoch, reference)
        control(rt, db, "RESUME", epoch, reference)
        result["canary"] = canary
        # Remove the synthetic scheduling delay only after verification and RESUME.
        db.execute("update trading_normalization_dispatches set next_attempt_at=clock_timestamp() where id=%s", (unsent_before["id"],))
        validate(rt, args, core, received_sid, validation_key)
        validate(rt, args, core, retry_sid, retry_key, "retry")
        checks[ITEMS[8]] = {"result": "PASS", "samePublicKeysAccepted": True, "verificationRef": reference}
        checkpoint()
        resumed = []
        for sid, key, endpoint in ((received_sid, validation_key, "validation"), (retry_sid, retry_key, "retry"), (unsent_sid, None, None)):
            published = rt.poll(lambda: state(rt, args, core, sid), lambda s: s["status"] == "ACCEPTED", args.timeout, "resumed publication")
            item = dispatch(rt, db, sid)
            run_id = str(item["run_id"])
            rt.poll(lambda: rt.row(db, "select status from trading_normalization_ack_dispatches where run_id=%s::uuid", (run_id,)),
                lambda a: a and a["status"] == "ACKNOWLEDGED", args.timeout, "resumed ACK")
            if key:
                validate(rt, args, core, sid, key, endpoint)
                validate(rt, args, core, sid, key, endpoint)
            counts = rt.row(db, """select (select count(*) from trading_normalization_runs where import_session_id=%s) as runs,
                (select count(*) from trading_normalization_dispatches where run_id=%s::uuid) as dispatches,
                (select count(*) from trading_ledger_revisions where run_id=%s::uuid) as revisions,
                (select count(*) from trading_normalization_ack_dispatches where run_id=%s::uuid) as acks""", (int(sid), run_id, run_id, run_id))
            jobs = rt.row(compute_db, "select count(*) as n from trading_review_jobs where core_normalization_run_id=%s", (run_id,))["n"]
            rt.require(counts == {"runs": 2 if sid == retry_sid else 1, "dispatches": 1, "revisions": 1, "acks": 1} and jobs == 1,
                "RESUME_DUPLICATED_WORK")
            resumed.append({"sessionId": sid, "runId": run_id, "jobId": item["compute_job_id"],
                "revisionId": published["createdLedgerRevisionId"], **counts, "jobs": jobs})
        checks[ITEMS[9]] = {"result": "PASS", "sessions": resumed}
        rt.require(all(v["result"] == "PASS" for v in checks.values()), "ORCH028_INCOMPLETE")
        result.update(result="PASS", epoch=epoch, mainCombination=False,
            implementationCoreCommit=rt.git_revision(args.core_path), uncertainCreateReplayed=False)
    finally:
        proxy.block_release.set()
        if admission_lock is not None:
            admission_lock.rollback()
            admission_lock.close()
        executor.shutdown(wait=True)
        service(rt, args, "stop-integration.ps1", "-RegistryId", secondary_registry, "-Service", "core-api")
        # A failed scenario deliberately leaves the original database gate paused.
        if result["result"] != "PASS":
            if "epoch" in locals() and not current(rt, db)["paused"]:
                control(rt, db, "PAUSE", epoch, "orch028-failure-after-resume")
            result["result"] = "FAIL"
            result["pausedAtFailure"] = current(rt, db)["paused"]
        (args.run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8")
