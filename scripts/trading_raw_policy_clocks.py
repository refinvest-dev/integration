"""Real policy-clock, queued/unknown-create expiry and raw-only preservation assertions.

Only the synthetic artifact deadline is shortened to advance the policy clock. No
Session/Run/generation/request/checkpoint or Compute job state is written by the harness.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def checkpoint(args, evidence):
    (args.run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8")


def accepted_clock(rt, args, evidence, core, db, sid, validation_key):
    from trading_normalization_cutover import validate

    def clocks():
        return db.execute("""select s.accepted_at,r.raw_expires_at,p.published_at from trading_import_sessions s
            join trading_source_artifacts r on r.import_session_id=s.id
            join trading_ledger_revisions p on p.id=s.created_ledger_revision_id
            where s.id=%s order by r.artifact_role""", (int(sid),)).fetchall()

    before = clocks()
    rt.require(len(before) == 2 and all(a == p and (expiry - a).total_seconds() == 604800 for a, expiry, p in before),
        "ACCEPTED_RAW_CLOCK_NOT_PINNED")
    validate(rt, args, core, sid, validation_key)
    rt.require(clocks() == before, "VALIDATION_REPLAY_EXTENDED_RAW_CLOCK")
    evidence.setdefault("rawPolicyClocks", {})["acceptedClock"] = {
        "result": "PASS", "scenarioId": "TR-ACC-DELETE-001", "replayPreserved": True,
    }
    checkpoint(args, evidence)


def queued_expiry(rt, args, evidence, core, compute, db, compute_db, proxy, book, artifacts):
    from trading_normalization_cutover import dispatch, new_session, validate

    results = evidence.setdefault("rawPolicyClocks", {})
    raw_root = Path(json.loads(os.environ["SPRING_APPLICATION_JSON"])["refinvest.trading-review.raw.root"])
    for unknown in (False, True):
        capacity = rt.row(compute_db, "select count(*) as n from trading_review_jobs where status in ('PENDING','RUNNING')")["n"]
        sid, _ = new_session(rt, args, core, book, artifacts)
        if unknown:
            proxy.drop_response_sessions.add(str(sid))
        validate(rt, args, core, sid)
        item = rt.poll(lambda: dispatch(rt, db, sid),
            lambda d: d and (d["last_failure_code"] == "CREATE_OUTCOME_UNKNOWN" if unknown else d["compute_job_id"]),
            args.timeout, "TTL create identity")
        run_id = str(item["run_id"])
        sent, accepted = proxy.creates[run_id]
        if unknown:
            rt.require(item["compute_job_id"] is None, "TTL_UNKNOWN_CREATE_WAS_ATTACHED")
        locators = db.execute("select object_key from trading_source_artifacts where import_session_id=%s", (int(sid),)).fetchall()
        db.execute("update trading_source_artifacts set raw_expires_at=clock_timestamp() where import_session_id=%s", (int(sid),))
        root = rt.poll(lambda: rt.row(db, """select raw_expired_at,deletion_generation,failure_code from
            trading_import_sessions where id=%s""", (int(sid),)), lambda r: r["raw_expired_at"] is not None,
            args.timeout, "Core policy expiry")
        rt.require(root["failure_code"] == "SOURCE_ARTIFACT_EXPIRED" and root["deletion_generation"] > sent["deletionGeneration"],
            "TTL_GENERATION_NOT_FENCED")
        request = rt.poll(lambda: rt.row(db, """select d.id,d.state from trading_deletion_requests d
            join trading_source_artifacts a on d.target_id=a.id where d.target_type='TRADING_SOURCE_ARTIFACT'
            and a.import_session_id=%s order by d.id limit 1""", (int(sid),)), lambda r: r is not None,
            args.timeout, "durable policy deletion")
        terminal = rt.poll(lambda: rt.request(core, "GET", args.core_url + f"/trading-deletion-requests/{request['id']}", 200),
            lambda d: d["state"] in {"COMPLETED", "FAILED"}, args.timeout, "verified TTL runtime absence")
        rt.require(terminal["state"] == "COMPLETED", "TTL_RUNTIME_NOT_REMOVED")
        rt.poll(lambda: rt.row(db, """select count(*) as n from trading_source_artifacts where import_session_id=%s
            and retention_status='DELETED' and object_key is null and wrapped_dek is null and key_version is null and filename is null""",
            (int(sid),))["n"], lambda n: n == 2, args.timeout, "TTL raw/key absence")
        state = rt.request(core, "GET", args.core_url + f"/trading-import-sessions/{sid}", 200)
        rt.require(state["status"] == "PROCESSING_FAILED" and state["failure"]["code"] == "SOURCE_ARTIFACT_EXPIRED",
            "TTL_SESSION_NOT_REUPLOAD_REQUIRED")
        rt.require(rt.row(compute_db, "select count(*) as n from trading_review_jobs where core_normalization_run_id=%s", (run_id,))["n"] == 0,
            "TTL_COMPUTE_JOB_REMAINS")
        rt.require(rt.row(compute_db, "select verified_absent_at from trading_normalization_deletions where core_normalization_run_id=%s", (run_id,))["verified_absent_at"] is not None,
            "TTL_RUNTIME_RECEIPT_MISSING")
        rt.require(all(not (raw_root / key).exists() for (key,) in locators), "TTL_RAW_FILE_REMAINS")
        rt.require(rt.row(db, "select count(*) as n from trading_ledger_revisions where import_session_id=%s", (int(sid),))["n"] == 0,
            "EXPIRED_INPUT_PUBLISHED")
        rt.require(rt.row(db, "select count(*) as n from trading_normalization_ack_dispatches where run_id=%s::uuid and disposition='PERSISTED'", (run_id,))["n"] == 0,
            "EXPIRED_INPUT_PERSISTED_ACK")
        rt.require(rt.row(compute_db, "select count(*) as n from trading_review_jobs where status in ('PENDING','RUNNING')")["n"] == capacity,
            "TTL_CAPACITY_NOT_RELEASED")
        replay = rt.request(compute, "POST", args.compute_url + "/trading-review/normalization-jobs", 409,
            headers={"Idempotency-Key": item["compute_key"]}, json=sent)
        rt.require(replay["code"] == "RUN_DELETED", "TTL_LATE_CREATE_NOT_FENCED")
        count = rt.row(db, """select count(*) as n from trading_deletion_requests d join trading_source_artifacts a
            on d.target_id=a.id where d.target_type='TRADING_SOURCE_ARTIFACT' and a.import_session_id=%s""", (int(sid),))["n"]
        rt.require(count == 1, "TTL_DUPLICATED_REQUEST")
        results["unknownCreateExpiry" if unknown else "queuedExpiry"] = {"result": "PASS", "sessionId": str(sid),
            "runId": run_id, "requestId": str(request["id"]), "state": terminal["state"], "lateCreateRejected": True,
            "metadataRetained": True, "scenarioIds": ["TR-ACC-DELETE-009", "TR-ACC-IMPORT-ORCH-011"]}
        checkpoint(args, evidence)


def accepted_purge(rt, args, evidence, core, db, sid):
    tables = ("trading_ledger_revisions", "trading_records", "trading_source_evidence_snapshots",
              "trading_ledger_analytics_supplement", "trading_books")

    def snapshot():
        return {table: rt.row(db, f"select md5(coalesce(string_agg(to_jsonb(t)::text,',' order by to_jsonb(t)::text),'')) as hash from {table} t")["hash"]
                for table in tables}

    before = snapshot()
    clock = rt.row(db, "select accepted_at from trading_import_sessions where id=%s", (int(sid),))["accepted_at"]
    db.execute("update trading_source_artifacts set raw_expires_at=clock_timestamp() where import_session_id=%s", (int(sid),))
    rt.poll(lambda: rt.row(db, "select count(*) as n from trading_source_artifacts where import_session_id=%s and retention_status='DELETED'", (int(sid),))["n"],
        lambda n: n == 2, args.timeout, "accepted raw TTL purge")
    state = rt.request(core, "GET", args.core_url + f"/trading-import-sessions/{sid}", 200)
    rt.require(state["status"] == "ACCEPTED" and snapshot() == before, "RAW_TTL_CHANGED_RETAINED_REVIEW")
    rt.require(rt.row(db, "select accepted_at,raw_expired_at from trading_import_sessions where id=%s", (int(sid),)) ==
        {"accepted_at": clock, "raw_expired_at": None}, "ACCEPTED_CLOCK_CHANGED_AFTER_PURGE")
    evidence["rawPolicyClocks"]["acceptedRawPurge"] = {"result": "PASS", "scenarioId": "TR-ACC-DELETE-001",
        "retainedReviewUnchanged": True, "acceptedClockPreserved": True}
    checkpoint(args, evidence)
