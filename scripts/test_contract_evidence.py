import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import contract_evidence as evidence
from contract_sync import ContractSyncError, REPOSITORIES


def candidate():
    heads = dict(zip(REPOSITORIES, (char * 40 for char in "bcde")))
    return {
        "schemaVersion": 2, "repository": "example/integration", "runId": 42,
        "runAttempt": 2, "workflowSha": "9" * 40, "contextRevision": "a" * 40,
        "heads": heads, "bases": {**heads, "coreApi": "f" * 40},
        "pullRequests": {"coreApi": {"number": 7, "head": heads["coreApi"], "base": "f" * 40}},
        "changedFiles": {"coreApi": ["docs/RULE.md", "CONTEXT_REVISION"]},
    }


def completed_run():
    return {"status": "completed", "conclusion": "success", "html_url": "https://example/run/42",
            "path": ".github/workflows/contract-propagation.yml", "head_branch": "main",
            "head_sha": "9" * 40, "event": "repository_dispatch", "run_attempt": 2,
            "repository": {"full_name": "example/integration"}}


def completed_jobs():
    return [{"name": f"{key} / {name}", "status": "completed", "conclusion": "success",
             "html_url": f"https://example/jobs/{key}"}
            for key, (_, name) in evidence.CHECKS.items()]


def checkout_evidence(manifest):
    repos = {key: {"available": True, "revision": sha, "worktree": "clean"}
             for key, sha in {"context": manifest["contextRevision"], **manifest["heads"]}.items()}
    return {key: {"verification": {"name": name, "outcome": "success"},
                  "validation": {"outcome": "success"}, "repositories": copy.deepcopy(repos)}
            for key, (_, name) in evidence.CHECKS.items()}


def live_pr(manifest):
    return {"number": 7, "state": "open", "body": evidence.render_body(manifest, "coreApi"),
            "head": {"sha": manifest["heads"]["coreApi"], "ref": evidence.BRANCH,
                     "repo": {"full_name": "example/core-api"}},
            "base": {"ref": "main", "repo": {"full_name": "example/core-api"}}}


class OutcomeTest(unittest.TestCase):
    def setUp(self):
        self.manifest = candidate()
        self.run = completed_run()
        self.jobs = completed_jobs()
        self.data = checkout_evidence(self.manifest)

    def outcome(self):
        return evidence.evaluate(self.manifest, self.run, self.jobs, self.data)

    def test_success_requires_completed_jobs_and_exact_five_checkout_shas(self):
        result = self.outcome()
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["checks"]["candidate-sync"]["checkedOutRevisions"]["web"], "d" * 40)

    def test_dispatch_and_running_are_pending_even_with_previous_success_data(self):
        for state in ("queued", "in_progress"):
            with self.subTest(state=state):
                self.run.update(status=state, conclusion=None)
                self.assertEqual(self.outcome()["outcome"], "pending")

    def test_failure_cancelled_and_timeout_are_distinct(self):
        for conclusion, workflow_conclusion in (("failure", "failure"), ("cancelled", "cancelled"),
                                                 ("timed_out", "failure")):
            with self.subTest(conclusion=conclusion):
                self.run["conclusion"] = workflow_conclusion
                self.jobs[0]["conclusion"] = conclusion
                self.assertEqual(self.outcome()["outcome"], conclusion)

    def test_cancel_before_jobs_start_is_cancelled_without_fabricated_checkout(self):
        self.run["conclusion"] = "cancelled"
        self.jobs = []
        self.data = {}
        self.assertEqual(self.outcome()["outcome"], "cancelled")
        self.assertEqual(self.outcome()["checks"]["candidate-sync"]["checkedOutRevisions"], {})

    def test_propagation_timeout_after_manifest_is_also_timeout(self):
        self.run["conclusion"] = "failure"
        self.jobs.append({"name": "propagate", "status": "completed", "conclusion": "timed_out"})
        self.assertEqual(self.outcome()["outcome"], "timed_out")

    def test_skipped_missing_ambiguous_and_in_progress_checks_never_pass(self):
        for mode in ("skipped", "missing", "ambiguous", "in_progress"):
            with self.subTest(mode=mode):
                self.jobs = completed_jobs()
                if mode == "missing":
                    self.jobs.pop()
                elif mode == "ambiguous":
                    self.jobs.append(copy.deepcopy(self.jobs[-1]))
                elif mode == "in_progress":
                    self.jobs[-1]["status"] = mode
                else:
                    self.jobs[-1]["conclusion"] = mode
                self.assertEqual(self.outcome()["outcome"], "failure")

    def test_missing_evidence_and_any_wrong_or_dirty_checkout_fail_closed(self):
        for key in ("context", *REPOSITORIES):
            with self.subTest(repository=key):
                self.data = checkout_evidence(self.manifest)
                self.data["candidate-smoke"]["repositories"][key]["revision"] = "0" * 40
                self.assertEqual(self.outcome()["outcome"], "failure")
        self.data = checkout_evidence(self.manifest)
        self.data["candidate-sync"]["repositories"]["web"]["worktree"] = "dirty"
        self.assertEqual(self.outcome()["outcome"], "failure")
        self.data = {}
        self.assertEqual(self.outcome()["outcome"], "failure")

    def test_inner_verification_or_validation_failure_never_passes(self):
        for field in ("verification", "validation"):
            self.data = checkout_evidence(self.manifest)
            self.data["candidate-sync"][field]["outcome"] = "failure"
            self.assertEqual(self.outcome()["outcome"], "failure")


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        self.manifest = candidate()
        self.context = self.manifest["contextRevision"]
        self.mains = self.manifest["bases"].copy()
        self.prs = {"coreApi": live_pr(self.manifest)}

    def errors(self):
        return evidence.freshness(self.manifest, self.context, self.mains, self.prs)

    def test_docs_only_pr_keeps_unchanged_repositories_in_tuple(self):
        self.assertEqual(self.errors(), [])
        body = evidence.render_body(self.manifest, "coreApi")
        for sha in self.manifest["heads"].values():
            self.assertIn(sha, body)
        self.assertIn("스냅샷 변경 없음", body)
        self.assertIn("**pending**", body)
        for heading in ("Context source", "Candidate set", "Verification evidence",
                        "Changed files", "Maintainer review"):
            self.assertIn("## " + heading, body)
        pending = copy.deepcopy(self.manifest)
        pending["pullRequests"] = {}
        self.assertIn("스냅샷 PR 연결 대기 중", evidence.render_body(pending, "coreApi"))

    def test_pr_head_change_invalidates_existing_success(self):
        self.prs["coreApi"]["head"]["sha"] = "1" * 40
        self.assertTrue(self.errors())

    def test_new_context_commit_invalidates_existing_success(self):
        self.context = "1" * 40
        self.assertTrue(self.errors())

    def test_changed_main_invalidates_with_and_without_snapshot_pr(self):
        for key in REPOSITORIES:
            with self.subTest(repository=key):
                self.mains = self.manifest["bases"].copy()
                self.mains[key] = "1" * 40
                self.assertTrue(self.errors())

    def test_closed_or_retargeted_pr_cannot_reuse_evidence(self):
        self.prs["coreApi"]["state"] = "closed"
        self.assertTrue(self.errors())
        self.prs["coreApi"] = live_pr(self.manifest)
        self.prs["coreApi"]["base"]["ref"] = "release"
        self.assertTrue(self.errors())


class CompletionObserverTest(unittest.TestCase):
    def setUp(self):
        self.manifest = candidate()
        self.run = completed_run()
        self.pr = live_pr(self.manifest)
        self.updates = []
        self.data = checkout_evidence(self.manifest)

    def api(self, endpoint, *, payload=None):
        if "/attempts/" in endpoint:
            return self.run
        if "/pulls/" in endpoint:
            if payload:
                self.updates.append(payload)
            return self.pr
        if "/context/" in endpoint:
            return {"object": {"sha": self.manifest["contextRevision"]}}
        for key, name in evidence.REMOTE_NAMES.items():
            if f"/{name}/git/ref/" in endpoint:
                return {"object": {"sha": self.manifest["bases"][key]}}
        self.fail(f"Unexpected endpoint: {endpoint}")

    def artifact(self, repository, artifacts, name, filename):
        if name == "contract-propagation-result-2":
            return self.manifest
        for key, (artifact, _) in evidence.CHECKS.items():
            if name == artifact + "-2":
                return self.data[key]
        self.fail(f"Unexpected artifact: {name}")

    def collect(self, output):
        with patch.object(evidence, "gh_json", side_effect=self.api), \
                patch.object(evidence, "pages", side_effect=lambda url, key: completed_jobs() if key == "jobs" else []), \
                patch.object(evidence, "artifact_json", side_effect=self.artifact):
            return evidence.reconcile("example/integration", 42, 2, output)

    def test_completed_run_updates_same_pr_and_persists_json_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.collect(Path(directory))
            self.assertEqual(result["outcome"], "success")
            self.assertEqual(result["freshness"], "current")
            self.assertEqual(len(self.updates), 1)
            self.assertIn("**success**", self.updates[0]["body"])
            saved = json.loads((Path(directory) / "contract-candidate-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["prUpdates"]["coreApi"], "updated")
            self.assertTrue((Path(directory) / "contract-candidate-evidence.md").is_file())

    def test_delayed_observer_never_overwrites_newer_pending_body_or_head(self):
        for change in ("body", "head"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                self.pr = live_pr(self.manifest)
                if change == "body":
                    self.pr["body"] = "<!-- contract-evidence:43:1 -->"
                else:
                    self.pr["head"]["sha"] = "0" * 40
                result = self.collect(Path(directory))
                self.assertEqual(self.updates, [])
                self.assertTrue(result["prUpdates"]["coreApi"].startswith("skipped:"))
                if change == "head":
                    self.assertEqual(result["freshness"], "stale")

    def test_observer_rejects_untrusted_workflow_branch_event_and_attempt(self):
        for field, value in (("head_branch", "feature"), ("event", "pull_request"),
                             ("path", ".github/workflows/other.yml"), ("run_attempt", 1)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                self.run = completed_run()
                self.run[field] = value
                with self.assertRaises(ContractSyncError):
                    self.collect(Path(directory))
                self.assertEqual(self.updates, [])

    def test_no_manifest_is_unconfirmed_and_does_not_write_pr(self):
        with patch.object(evidence, "gh_json", return_value=self.run), \
                patch.object(evidence, "pages", return_value=[]), \
                patch.object(evidence, "artifact_json", return_value=None), \
                tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ContractSyncError, "unconfirmed"):
                evidence.reconcile("example/integration", 42, 2, Path(directory))
            saved = json.loads((Path(directory) / "contract-candidate-evidence.json").read_text(encoding="utf-8"))
            self.assertFalse(saved["tupleAvailable"])
            self.assertEqual(saved["freshness"], "unconfirmed")
            self.assertEqual(saved["outcome"], "failure")

    def test_cancel_or_timeout_before_manifest_records_termination_without_inventing_shas(self):
        for conclusion in ("cancelled", "timed_out"):
            run = completed_run()
            run["conclusion"] = "cancelled" if conclusion == "cancelled" else "failure"
            jobs = [{"name": "propagate", "conclusion": conclusion}]
            with self.subTest(conclusion=conclusion), tempfile.TemporaryDirectory() as directory, \
                    patch.object(evidence, "gh_json", return_value=run), \
                    patch.object(evidence, "pages", side_effect=lambda url, key: jobs if key == "jobs" else []), \
                    patch.object(evidence, "artifact_json", return_value=None):
                with self.assertRaisesRegex(ContractSyncError, "unconfirmed"):
                    evidence.reconcile("example/integration", 42, 2, Path(directory))
                saved = json.loads((Path(directory) / "contract-candidate-evidence.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["outcome"], conclusion)
                self.assertIsNone(saved["heads"])

    def test_freshness_api_failure_preserves_job_result_as_unconfirmed(self):
        original = self.api
        def api(endpoint, *, payload=None):
            if "/context/" in endpoint:
                raise ContractSyncError("Context read unavailable")
            return original(endpoint, payload=payload)
        self.api = api
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ContractSyncError, "unavailable"):
                self.collect(Path(directory))
            saved = json.loads((Path(directory) / "contract-candidate-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["outcome"], "success")
            self.assertEqual(saved["freshness"], "unconfirmed")
            self.assertEqual(self.updates, [])

    def test_pr_write_failure_does_not_erase_final_evidence(self):
        original = self.api
        def api(endpoint, *, payload=None):
            if payload:
                raise ContractSyncError("PR write unavailable")
            return original(endpoint, payload=payload)
        self.api = api
        with tempfile.TemporaryDirectory() as directory:
            result = self.collect(Path(directory))
            self.assertTrue(result["prUpdates"]["coreApi"].startswith("failed:"))
            saved = json.loads((Path(directory) / "contract-candidate-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["outcome"], "success")
            self.assertEqual(saved["prUpdates"], result["prUpdates"])

    def test_manifest_run_attempt_and_tuple_are_validated(self):
        for field, value in (("runAttempt", 1), ("repository", "other/integration"),
                             ("contextRevision", "main"), ("heads", {})):
            manifest = candidate()
            manifest[field] = value
            with self.subTest(field=field), self.assertRaises(ContractSyncError):
                evidence.validate_manifest(manifest, "example/integration", 42, 2)


class ArtifactTest(unittest.TestCase):
    def test_json_is_read_without_extracting_archive_files(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("compatibility.json", '{"ok":true}')
            bundle.writestr("../../untrusted.py", "raise RuntimeError('must not run')")
        item = {"name": "evidence-2", "expired": False, "id": 12, "size_in_bytes": len(buffer.getvalue())}
        with patch.object(evidence.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=buffer.getvalue(), stderr=b"")):
            self.assertEqual(evidence.artifact_json("example/integration", [item], "evidence-2", "compatibility.json"), {"ok": True})

    def test_expired_missing_and_ambiguous_artifacts_are_not_success(self):
        self.assertIsNone(evidence.artifact_json("example/integration", [], "evidence-2", "compatibility.json"))
        item = {"name": "evidence-2", "expired": False, "id": 12, "size_in_bytes": 10}
        with self.assertRaises(ContractSyncError):
            evidence.artifact_json("example/integration", [item, item], "evidence-2", "compatibility.json")


if __name__ == "__main__":
    unittest.main()
