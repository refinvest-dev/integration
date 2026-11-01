"""Real HTTP absence receipts, unknown create outcomes, and late-create fences.

Runs while the actual Compute worker is stopped, using fresh synthetic Books.
No Core/Compute business state is replaced or advanced by test-side SQL.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid


def verify_queued_deletions(rt, args, evidence, core, compute, db, compute_db, proxy, book, artifacts):
    from trading_normalization_cutover import dispatch, new_session, validate

    assertions = evidence.setdefault("runtimeDeletion", {})
    raw_root = Path(json.loads(os.environ["SPRING_APPLICATION_JSON"])["refinvest.trading-review.raw.root"])
    for unknown in (False, True):
        before = rt.row(compute_db,
            "select count(*) as n from trading_review_jobs where status in ('PENDING','RUNNING')", ())["n"]
        sid, uploaded = new_session(rt, args, core, book, artifacts)
        if unknown:
            proxy.drop_response_sessions.add(str(sid))
        validate(rt, args, core, sid)
        run = rt.poll(lambda: dispatch(rt, db, sid),
            lambda d: d and (d["last_failure_code"] == "CREATE_OUTCOME_UNKNOWN" if unknown else d["compute_job_id"]),
            args.timeout, "deletion fixture create identity")
        run_id = str(run["run_id"])
        sent, accepted = proxy.creates[run_id]
        if unknown:
            rt.require(run["compute_job_id"] is None, "DELETION_UNKNOWN_JOB_WAS_ATTACHED")
        locators = db.execute("select object_key from trading_source_artifacts where import_session_id=%s",
            (int(sid),)).fetchall()
        deletion = rt.request(core, "DELETE", args.core_url + f"/trading-import-sessions/{sid}", 202,
            headers={"Idempotency-Key": "runtime-delete-" + uuid.uuid4().hex})
        rt.require(core.get(args.core_url + f"/trading-import-sessions/{sid}").status_code == 410,
            "DELETION_ACCESS_NOT_REVOKED")
        terminal = rt.poll(lambda: rt.request(core, "GET",
            args.core_url + f"/trading-deletion-requests/{deletion['id']}", 200),
            lambda d: d["state"] in {"COMPLETED", "FAILED"}, args.timeout, "verified runtime deletion")
        rt.require(terminal["state"] == "COMPLETED", "RUNTIME_DELETION_NOT_COMPLETED")
        public_components = None
        if getattr(args, "raw_policy_clocks", False):
            public_components = {name: terminal[name] for name in (
                "primaryPurgeStatus", "objectPurgeStatus", "computeRuntimePurgeStatus", "backupPurgeStatus")}
            rt.require(public_components == {"primaryPurgeStatus": "COMPLETED", "objectPurgeStatus": "COMPLETED",
                "computeRuntimePurgeStatus": "COMPLETED", "backupPurgeStatus": "IN_PROGRESS"}
                and terminal["retryable"] is False and terminal["failure"] is None,
                "DELETION_PUBLIC_COMPONENT_PROGRESS_DRIFT")
        rt.require(rt.row(compute_db,
            "select count(*) as n from trading_review_jobs where core_normalization_run_id=%s", (run_id,))["n"] == 0,
            "DELETED_COMPUTE_JOB_REMAINS")
        rt.require(compute.get(args.compute_url + f"/trading-review/normalization-jobs/{accepted['jobId']}").status_code == 404,
            "DELETED_COMPUTE_JOB_READABLE")
        rt.require(rt.row(compute_db,
            "select verified_absent_at from trading_normalization_deletions where core_normalization_run_id=%s",
            (run_id,))["verified_absent_at"] is not None, "COMPUTE_ABSENCE_RECEIPT_MISSING")
        rt.require(all(not (raw_root / row[0]).exists() for row in locators),
            "DELETED_RAW_OBJECT_REMAINS")
        rt.require(rt.row(db, "select count(*) as n from trading_import_sessions where id=%s", (int(sid),))["n"] == 0,
            "DELETED_PRIMARY_REMAINS")
        rt.require(rt.row(db, "select count(*) as n from trading_deletion_checkpoints where request_id=%s",
            (int(deletion["id"]),))["n"] == 6, "DELETION_CHECKPOINTS_INCOMPLETE")
        rt.require(rt.row(compute_db,
            "select count(*) as n from trading_review_jobs where status in ('PENDING','RUNNING')", ())["n"] == before,
            "DELETION_CAPACITY_NOT_RELEASED")
        rejected = rt.request(compute, "POST", args.compute_url + "/trading-review/normalization-jobs", 409,
            headers={"Idempotency-Key": run["compute_key"]}, json=sent)
        rt.require(rejected["code"] == "RUN_DELETED", "LATE_CREATE_NOT_FENCED")
        assertions["unknownCreate" if unknown else "queuedJob"] = {
            "result": "PASS", "scenarioIds": ["TR-ACC-DELETE-006", "TR-ACC-DELETE-009", "TR-ACC-DELETE-012"],
            "sessionId": str(sid), "runId": run_id, "deletionRequestId": deletion["id"],
            "state": terminal["state"], "checkpoints": 6, "lateCreateRejected": True,
            "artifacts": sorted(value["id"] for value in uploaded.values()),
        }
        if public_components is not None:
            assertions["unknownCreate" if unknown else "queuedJob"]["publicComponents"] = public_components
