"""Ordered PCP-12 drills; public policy remains in Context ADR-086."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import time
import uuid

from owner_query_e2e import ACCOUNT, ISSUER, MEMBER, TRACE_PARENT, free_port, private_write, require
from owner_query_fleet_relay import AUTHORITY_PATH


def token_parts(token):
    # Scheduling/probe construction only. Real services must validate every signature and claim.
    require(isinstance(token, str) and len(token) <= 4096 and token.count(".") == 2, "MISSING_CAPTURED_DELEGATION")
    return [json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))) for part in token.split(".")[:2]]


class FleetScenarios:
    def scenarios(self):
        from owner_query_fleet_e2e import OLD_KID, NEW_KID

        self.mark("two_core_two_go_eight_callback_combinations_and_both_metadata_resources")
        both_keys = self.public_keys([OLD_KID, NEW_KID])
        both_callbacks = list(self.callback_credentials.values())
        both_ingress = list(self.ingress_credentials.values())
        self.checkpoint("VERIFIER_PREPARATION")
        for label in ("core-a", "core-b"):
            self.roll(label, keys=both_keys, authorityCredentials=both_callbacks)
        self.roll("go-a", keys=both_keys, callers=[{"id": "core-api", "credentials": both_ingress, "readAllowed": True}])
        self.roll("go-b", callers=[{"id": "core-api", "credentials": both_ingress, "readAllowed": True}])
        self.metadata_relay.targets.exclude("go-b")
        self.nodes["go-b"].serving = False
        self.roll("core-a", signingKid=NEW_KID, privateKey=self.keys[NEW_KID]["private"],
                  goCredential=self.ingress_credentials["new"])
        self.metadata_relay.targets.select("go-a")
        self.request_node(self.nodes["core-a"])
        new_key_token = self.metadata_relay.token.peek()
        self.metadata_relay.targets.select("go-b", diagnostic=True)
        self.authority_relay.targets.select("core-a")
        before = self.authority_relay.counts["forwarded"]
        self.request_node(self.nodes["core-a"], status=502, code="UPSTREAM_SERVICE_ERROR")
        probe = self.session.get(self.nodes["go-b"].origin + "/internal/trading-review/v1/accounts/" + ACCOUNT, headers={
            "Authorization": "Bearer " + self.ingress_credentials["new"],
            "X-RefInvest-User-Delegation": new_key_token, "traceparent": TRACE_PARENT}, timeout=(2, 8))
        require(probe.status_code == 401 and probe.json().get("code") == "USER_DELEGATION_INVALID", "STALE_NODE_NOT_UNKNOWN_KEY_FAILURE")
        require(self.authority_relay.counts["forwarded"] == before, "UNKNOWN_KID_REACHED_AUTHORITY")
        require(self.session.get(self.nodes["go-b"].origin + "/readyz", timeout=3).status_code == 200, "STALE_NODE_NOT_TECHNICALLY_READY")
        try:
            self.metadata_relay.targets.select("go-b")
        except ValueError:
            pass
        else:
            require(False, "UNCONFIRMED_NODE_WAS_ROUTED")
        self.metadata_relay.targets.exclude("go-b")
        self.request_node(self.nodes["core-a"])
        self.mark("unprepared_but_ready_node_is_excluded_and_unknown_kid_has_no_fallback")
        self.roll("go-b", keys=both_keys, callers=[{"id": "core-api", "credentials": both_ingress, "readAllowed": True}],
                  authorityCredential=self.callback_credentials["new"])
        self.matrix("mixed-signers-and-two-way-credential-overlap")
        self.mark("new_key_is_verified_before_all_issuers_switch_and_both_credentials_overlap")

        self.checkpoint("LAST_OLD_ISSUER")
        self.metadata_relay.targets.select("go-a")
        self.authority_relay.targets.select("core-a")
        self.request_node(self.nodes["core-b"])
        old_token = self.metadata_relay.token.peek()
        header, claims = token_parts(old_token)
        require(header["kid"] == OLD_KID, "OLD_ISSUER_CAPTURE_MISMATCH")
        self.roll("core-b", signingKid=NEW_KID, privateKey=self.keys[NEW_KID]["private"],
                  goCredential=self.ingress_credentials["new"])
        last_old_stopped = self.nodes["core-b"].last_stopped_at
        self.replay_all(old_token, 200)
        self.matrix("all-new-issuers-with-old-key-verify-only")
        self.mark("last_old_issuer_drains_and_unexpired_old_delegation_remains_verify_only")

        self.invalid_replacements()
        self.checkpoint("AUTHORITY_COMMIT")
        self.sql(f"UPDATE auth_member_access SET allowed=FALSE,revision=revision+1 WHERE member_id={MEMBER};")
        self.denied_matrix()
        self.sql(f"UPDATE auth_member_access SET allowed=TRUE,revision=revision+1 WHERE member_id={MEMBER};")
        self.matrix("member-explicit-fixture-restoration")
        self.mark("member_block_commit_applies_across_current_fleet_without_positive_cache")
        self.sql("UPDATE auth_authority_callers SET enabled=FALSE,revision=revision+1 WHERE caller_id='core-api';")
        self.denied_matrix()
        self.sql("UPDATE auth_authority_callers SET enabled=TRUE,revision=revision+1 WHERE caller_id='core-api';")
        self.mark("ingress_caller_block_commit_applies_across_authority_nodes")

        # An adversarial fixture signer retains the retired material; it is never a serving issuer.
        attack = self.old_key_probe(old_token)
        self.replay_all(attack, 200)
        self.sql(f"UPDATE auth_authority_keys SET enabled=FALSE,revision=revision+1 WHERE issuer='{ISSUER}' AND kid='{OLD_KID}';")
        self.replay_all(attack, 403, "MEMBER_ACCESS_DENIED")
        self.mark("revoked_old_kid_is_denied_while_verifiers_still_hold_public_key")

        self.matrix("fresh-new-delegations-before-specific-revocation")
        token = self.metadata_relay.token.peek()
        _, fresh = token_parts(token)
        fingerprint = hashlib.sha256(fresh["jti"].encode("utf-8")).hexdigest()
        self.sql(f"INSERT INTO auth_authority_revocations VALUES ('{ISSUER}','{fingerprint}',to_timestamp({fresh['exp'] + 5}));")
        self.replay_all(token, 403, "MEMBER_ACCESS_DENIED")
        self.matrix("fresh-jti-is-not-overblocked")
        self.mark("specific_jti_revocation_commits_without_blocking_fresh_delegations")

        # Fixture bound: stop time + maximum TTL + both skew directions + longest request budget.
        retire_after = max(last_old_stopped + 60 + 2 * 5 + 12, claims["exp"] + 2 * 5 + 12)
        self.checkpoint("VERIFY_ONLY_RETIREMENT_WINDOW")
        waited = time.monotonic()
        while time.time() < retire_after:
            require(time.monotonic() - waited < 95, "RETIREMENT_WINDOW_NOT_BOUNDED")
            time.sleep(max(0.01, min(1, retire_after - time.time())))
        self.evidence["retirementWindow"] = {"ttlSeconds": 60, "leewayEachDirectionSeconds": 5,
            "longestFixtureRequestBudgetSeconds": 12, "elapsedAfterOldIssuerStoppedSeconds": round(time.time() - last_old_stopped, 1),
            "operationalDurationSelected": False}
        self.replay_all(old_token, 401, "USER_DELEGATION_INVALID")
        self.mark("real_clock_wait_covers_ttl_both_skews_and_fixture_request_budget")
        self.checkpoint("OLD_MATERIAL_RETIREMENT")
        for label in ("go-a", "go-b"):
            self.roll(label, keys=self.public_keys([NEW_KID]),
                callers=[{"id": "core-api", "credentials": [self.ingress_credentials["new"]], "readAllowed": True}],
                authorityCredential=self.callback_credentials["new"])
        for label in ("core-a", "core-b"):
            self.roll(label, keys=self.public_keys([NEW_KID]), authorityCredentials=[self.callback_credentials["new"]])
        self.matrix("only-new-material-after-retirement")
        new_token = self.metadata_relay.token.peek()
        self.retired_credentials(new_token)
        self.mark("retired_directional_credentials_are_rejected_and_new_credentials_remain_usable")
        before = self.authority_relay.counts["forwarded"]
        self.replay_all(self.old_key_probe(old_token), 401, "USER_DELEGATION_INVALID")
        require(self.authority_relay.counts["forwarded"] == before, "RETIRED_KEY_REACHED_AUTHORITY")
        self.matrix("final-confirmed-fleet")
        self.mark("fresh_old_key_attack_is_rejected_before_authority_after_public_key_retirement")
        old_token = token = attack = new_token = None
        self.metadata_relay.token.clear()
        require(self.sql("SELECT count(*) FROM trading_idempotency;") == "0" and
                self.sql("SELECT count(*) FROM trading_import_sessions;") == "0", "FLEET_READ_CREATED_DURABLE_WORK")
        self.mark("fleet_reads_create_no_idempotency_or_job_and_token_slots_are_cleared")

    def replay_all(self, token, status, code=None):
        for go in self.role_nodes("go"):
            for authority in self.role_nodes("core"):
                self.authority_relay.targets.select(authority.label)
                response = self.session.get(go.origin + "/internal/trading-review/v1/accounts/" + ACCOUNT, headers={
                    "Authorization": "Bearer " + self.ingress_credentials["new"],
                    "X-RefInvest-User-Delegation": token, "traceparent": TRACE_PARENT}, timeout=(2, 10))
                require(response.status_code == status, "REPLAY_STATUS_" + str(response.status_code))
                require(response.headers.get("Cache-Control") == "no-store", "REPLAY_CACHE_POLICY")
                if code:
                    require(response.json().get("code") == code, "REPLAY_ERROR_CODE")

    def denied_matrix(self):
        for core in self.role_nodes("core"):
            for go in self.role_nodes("go"):
                self.metadata_relay.targets.select(go.label)
                require(self.session.get(go.origin + "/readyz", timeout=3).status_code == 200, "AUTHORITY_DENIAL_CHANGED_READY")
                for authority in self.role_nodes("core"):
                    self.authority_relay.targets.select(authority.label)
                    self.request_node(core, status=403, code="MEMBER_ACCESS_DENIED")

    def retired_credentials(self, token):
        for go in self.role_nodes("go"):
            response = self.session.get(go.origin + "/internal/trading-review/v1/accounts/" + ACCOUNT, headers={
                "Authorization": "Bearer " + self.ingress_credentials["old"], "X-RefInvest-User-Delegation": token,
                "traceparent": TRACE_PARENT}, timeout=(2, 8))
            require(response.status_code == 401, "OLD_INGRESS_CREDENTIAL_STILL_ACCEPTED")
        for core in self.role_nodes("core"):
            for generation, expected in (("old", 401), ("new", 200)):
                response = self.session.post(core.origin + AUTHORITY_PATH, headers={
                    "Authorization": "Bearer " + self.callback_credentials[generation], "traceparent": TRACE_PARENT},
                    json={"checkId": uuid.uuid4().hex, "expectedActor": "core-api", "delegation": token}, timeout=(2, 8))
                require(response.status_code == expected, "RETIRED_AUTHORITY_CREDENTIAL_STATUS")
                if expected == 200:
                    require(response.json().get("active") is True, "NEW_AUTHORITY_CREDENTIAL_NOT_ACTIVE")

    def old_key_probe(self, source):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from owner_query_fleet_e2e import OLD_KID

        header, claims = token_parts(source)
        issued_at = int(time.time())
        claims.update(iat=issued_at, exp=issued_at + 60, jti=uuid.uuid4().hex)
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
        signing = encode(header) + "." + encode(claims)
        key = serialization.load_pem_private_key(self.keys[OLD_KID]["private"].encode("ascii"), password=None)
        signature = key.sign(signing.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return signing + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")

    def invalid_replacements(self):
        from owner_query_fleet_e2e import FleetNode
        import requests

        saved = {}
        marker = "synthetic-private-invalid-snapshot-marker"
        self.secrets.add(marker)
        try:
            for label in ("core-a", "go-b"):
                node = self.nodes[label]
                saved[label] = (node.directory / "runtime.json").read_text(encoding="utf-8")
                invalid = dict(node.config, fixtureUnknownField=marker)
                private_write(node.directory / "runtime.json", invalid)
            self.matrix("file-mutation-does-not-reload-running-processes")
            for label in ("core-a", "go-b"):
                original = self.nodes[label]
                candidate = FleetNode(original.role + "-rejected", free_port(), self.directory / (original.role + "-rejected"))
                candidate.config = dict(copy.deepcopy(original.config), fixtureUnknownField=marker)
                self.write_node(candidate)
                process, log = self.spawn(candidate.label, self.command(candidate), candidate.env)
                deadline = time.monotonic() + 60
                while process.poll() is None and time.monotonic() < deadline:
                    try:
                        response = self.session.get(candidate.origin + ("/readyz" if candidate.role == "go" else "/actuator/health"), timeout=(0.2, 0.2))
                        require(response.status_code != 200, "REJECTED_SNAPSHOT_BECAME_READY")
                    except requests.RequestException:
                        pass
                    time.sleep(0.1)
                if process.poll() is None:
                    self.stop(process)
                    require(False, "INVALID_REPLACEMENT_DID_NOT_EXIT")
                require(process.returncode != 0, "INVALID_REPLACEMENT_SUCCEEDED")
                text = log.read_text(encoding="utf-8", errors="replace")
                expected = "Owner query preparation failed: SNAPSHOT" if candidate.role == "core" else "RUNTIME_STARTUP_FAILED"
                require(expected in text and marker not in text, "INVALID_REPLACEMENT_REASON_OR_REDACTION")
                if candidate.role == "go":
                    require('"stage":"resources"' in text or '"stage": "resources"' in text, "REJECTED_GO_NOT_CONFIGURATION_FAILURE")
            self.matrix("serving-fleet-survives-rejected-replacements")
            self.mark("invalid_replacements_exit_unready_without_mutating_existing_runtime_snapshots")
        finally:
            for label, content in saved.items():
                private_write(self.nodes[label].directory / "runtime.json", content)
