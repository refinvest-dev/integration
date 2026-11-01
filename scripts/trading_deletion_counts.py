"""Request-time count snapshots verified through real Core commands, publication and purge."""
import json
import uuid


def counts(accounts=0, books=0, sessions=0, artifacts=0, revisions=0):
    return dict(accounts=accounts, books=books, importSessions=sessions, artifacts=artifacts,
                ledgerRevisions=revisions, analysisRuns=0, reprocessingRuns=0, snapshotStatus="REQUEST_TIME_SNAPSHOT")


def verify_count_snapshots(rt, args, evidence, core, db, artifacts):
    from trading_normalization_cutover import new_session, validate, worker_poll

    account = rt.request(core, "POST", args.core_url + "/trading-accounts", 201,
        headers={"Idempotency-Key": "count-account-" + uuid.uuid4().hex},
        json={"venue": "BINANCE", "displayName": "Synthetic count snapshot"})
    sessions = [new_session(rt, args, core, {"tradingAccountId": account["id"]}, artifacts)[0] for _ in range(3)]
    first = rt.request(core, "GET", args.core_url + f"/trading-import-sessions/{sessions[0]}", 200)
    validate(rt, args, core, sessions[0])
    published = worker_poll(rt, args,
        lambda: rt.request(core, "GET", args.core_url + f"/trading-import-sessions/{sessions[0]}", 200),
        lambda s: s["status"] in {"ACCEPTED", "PROCESSING_FAILED", "REJECTED"}, args.timeout, "count fixture publication")
    rt.require(published["status"] == "ACCEPTED" and published["createdLedgerRevisionId"], "COUNT_PUBLICATION_FAILED")

    def delete_and_replay(path, expected, name):
        key = "count-delete-" + uuid.uuid4().hex
        initial = rt.request(core, "DELETE", path, 202, headers={"Idempotency-Key": key})
        rt.require(initial["affectedResources"] == expected, "REQUEST_COUNT_SNAPSHOT_DRIFT")
        terminal = rt.poll(lambda: rt.request(core, "GET",
            args.core_url + f"/trading-deletion-requests/{initial['id']}", 200),
            lambda r: r["state"] in {"COMPLETED", "FAILED"}, args.timeout, "count fixture purge")
        rt.require(terminal["state"] == "COMPLETED" and terminal["affectedResources"] == expected,
            "PURGE_CHANGED_REQUEST_COUNTS")
        replay = rt.request(core, "DELETE", path, 202, headers={"Idempotency-Key": key})
        rt.require(replay["id"] == initial["id"] and replay["affectedResources"] == expected,
            "POST_PURGE_REPLAY_CHANGED_COUNTS")
        evidence.setdefault("deletionCountSnapshots", {})[name] = {
            "result": "PASS", "requestId": initial["id"], "counts": expected, "purgedReplayPreserved": True,
        }
        (args.run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")

    delete_and_replay(args.core_url + f"/trading-books/{first['tradingBookId']}", counts(books=1, sessions=1, artifacts=2, revisions=1), "book")
    rt.require(rt.row(db, "SELECT count(*) AS n FROM trading_ledger_revisions WHERE id=%s::uuid",
        (published["createdLedgerRevisionId"],))["n"] == 0, "COUNT_REVISION_NOT_PURGED")
    delete_and_replay(args.core_url + f"/trading-accounts/{account['id']}", counts(accounts=1, books=2, sessions=2, artifacts=4), "account")
    rt.require(rt.row(db, "SELECT count(*) AS n FROM trading_accounts WHERE id=%s", (int(account["id"]),))["n"] == 0,
        "COUNT_ACCOUNT_NOT_PURGED")
