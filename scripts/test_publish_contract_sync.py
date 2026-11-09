"""Exercise publishing against local bare remotes; GitHub writes are mocked."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import publish_contract_sync as publisher
from contract_sync import ContractSyncError, REPOSITORIES


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE).decode().strip()


class PublishingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context = self.repository("context")
        (self.context / "docs").mkdir()
        (self.context / "docs/RULE.md").write_text("first\n")
        self.commit(self.context, "first")
        self.first = git(self.context, "rev-parse", "HEAD")
        self.roots = {}
        for key in REPOSITORIES:
            root = self.repository(key)
            (root / "CONTEXT_REVISION").write_text(self.first + "\n")
            if key == "coreApi":
                (root / "docs").mkdir()
                (root / "docs/RULE.md").write_text("first\n")
            self.commit(root, "initial")
            self.roots[key] = root
        (self.context / "docs/RULE.md").write_text("second\n")
        self.commit(self.context, "second")
        self.second = git(self.context, "rev-parse", "HEAD")
        self.manifest = {
            "schemaVersion": 1,
            "repositories": {key: {"revision": "CONTEXT_REVISION"} for key in REPOSITORIES},
            "mappings": [{"sourceDirectory": "docs", "files": "*.md", "mode": "text",
                          "targets": [{"repository": "coreApi", "directory": "docs"}]}],
            "unmappedContractPrefixes": ["docs/"], "informationalFiles": [],
        }
        self.bodies = []

    def repository(self, name):
        root = self.root / name
        root.mkdir()
        remote = self.root / (name + ".git")
        remote.mkdir()
        git(remote, "init", "--bare", "-q", "--initial-branch=main")
        git(root, "init", "-q", "--initial-branch=main")
        git(root, "config", "user.name", "Contract test")
        git(root, "config", "user.email", "test@example.invalid")
        git(root, "remote", "add", "origin", str(remote))
        return root

    def commit(self, root, title):
        git(root, "add", "-A")
        git(root, "commit", "-qm", title)
        git(root, "push", "-q", "origin", "HEAD")

    def github(self, root, owner, key, result):
        self.bodies.append(publisher.render_body(result, key))
        return {"number": 7 + REPOSITORIES.index(key), "head": result["heads"][key], "base": result["bases"][key]}

    def publish(self, revision=None):
        with patch.object(publisher, "load_map", return_value=self.manifest), \
                patch.object(publisher, "upsert_pr", side_effect=self.github), \
                patch.dict(os.environ, {"GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "9" * 40}):
            return publisher.publish(self.context, revision or self.second, self.roots, "example")

    def test_docs_only_publish_updates_one_pr_and_pins_all_other_mains(self):
        result = self.publish()
        self.assertEqual(set(result["pullRequests"]), {"coreApi"})
        self.assertEqual(set(result["heads"]), set(REPOSITORIES))
        self.assertTrue(result["runApiSmoke"], "Normative documents must also run smoke")
        self.assertEqual(result["outcome"], "pending")
        self.assertEqual(result["contextRevision"], self.second)
        self.assertEqual(len(self.bodies), 2)
        self.assertIn("[\u00237]", self.bodies[-1])
        self.assertEqual(set(result["changedFiles"]["coreApi"]), {"docs/RULE.md", "CONTEXT_REVISION"})
        for key in REPOSITORIES:
            if key != "coreApi":
                self.assertEqual(result["heads"][key], result["bases"][key])
        self.assertEqual(git(self.roots["coreApi"], "show", "HEAD:docs/RULE.md"), "second")

    def test_no_snapshot_change_still_records_full_tuple_without_pr(self):
        # An informational-only Context change creates no snapshot PRs.
        core = self.roots["coreApi"]
        (core / "docs/RULE.md").write_text("second\n")
        (core / "CONTEXT_REVISION").write_text(self.second + "\n")
        self.commit(core, "already synchronized")
        (self.context / "AGENTS.md").write_text("informational\n")
        self.commit(self.context, "info")
        revision = git(self.context, "rev-parse", "HEAD")
        result = self.publish(revision)
        self.assertEqual(result["pullRequests"], {})
        self.assertEqual(result["heads"], result["bases"])
        self.assertEqual(self.bodies, [])

    def test_multiple_changed_repositories_link_the_whole_set(self):
        self.manifest["mappings"][0]["targets"].append({"repository": "web", "directory": "docs"})
        result = self.publish()
        self.assertEqual(set(result["pullRequests"]), {"coreApi", "web"})
        for body in self.bodies[-2:]:
            self.assertIn("example/core-api/pull/7", body)
            self.assertIn("example/web/pull/9", body)
            for sha in result["heads"].values():
                self.assertIn(sha, body)

    def test_old_context_commit_is_rejected_before_any_target_push(self):
        with self.assertRaisesRegex(ContractSyncError, "main changed"):
            self.publish(self.first)
        for root in self.roots.values():
            self.assertEqual(git(root, "branch", "--show-current"), "main")

    def test_mutable_context_ref_is_rejected(self):
        with self.assertRaisesRegex(ContractSyncError, "full Context"):
            self.publish("main")

    def test_dirty_target_fails_before_applying_snapshots(self):
        (self.roots["coreApi"] / "user-work.txt").write_text("preserve\n")
        with self.assertRaisesRegex(ContractSyncError, "not clean"):
            self.publish()
        self.assertEqual((self.roots["coreApi"] / "user-work.txt").read_text(), "preserve\n")

    def test_existing_bot_branch_with_business_code_is_rejected(self):
        root = self.roots["coreApi"]
        git(root, "switch", "-c", publisher.BRANCH)
        (root / "business.py").write_text("not a snapshot\n")
        self.commit(root, "unexpected code")
        git(root, "switch", "main")
        with self.assertRaisesRegex(ContractSyncError, "non-snapshot"):
            self.publish()

    def test_stale_bot_snapshot_conflicts_refresh_without_losing_main_or_history(self):
        root = self.roots["coreApi"]
        git(root, "switch", "-c", publisher.BRANCH)
        (root / "docs/RULE.md").write_text("pending bot snapshot\n")
        (root / "CONTEXT_REVISION").write_text(self.second + "\n")
        self.commit(root, "pending snapshot")
        old_bot = git(root, "rev-parse", "HEAD")
        git(root, "switch", "main")
        (self.context / "docs/RULE.md").write_text("reviewed main snapshot\n")
        self.commit(self.context, "reviewed policy")
        third = git(self.context, "rev-parse", "HEAD")
        (root / "docs/RULE.md").write_text("reviewed main snapshot\n")
        (root / "CONTEXT_REVISION").write_text(third + "\n")
        (root / "business.py").write_text("main business change\n")
        self.commit(root, "main snapshot and business change")
        main = git(root, "rev-parse", "HEAD")
        (self.context / "docs/RULE.md").write_text("latest approved snapshot\n")
        self.commit(self.context, "latest policy")
        latest = git(self.context, "rev-parse", "HEAD")

        # A new CI checkout has the remote bot branch but no local branch.
        clone = self.root / "fresh-core"
        git(self.root, "clone", "-q", str(self.root / "coreApi.git"), str(clone))
        git(clone, "config", "user.name", "Contract test")
        git(clone, "config", "user.email", "test@example.invalid")
        self.roots["coreApi"] = clone
        result = self.publish(latest)

        self.assertEqual((clone / "docs/RULE.md").read_text(), "latest approved snapshot\n")
        self.assertEqual((clone / "CONTEXT_REVISION").read_text().strip(), latest)
        self.assertEqual((clone / "business.py").read_text(), "main business change\n")
        self.assertEqual(git(clone, "ls-files", "--unmerged"), "")
        for ancestor in [old_bot, main]:
            git(clone, "merge-base", "--is-ancestor", ancestor, "HEAD")
        self.assertEqual(set(result["changedFiles"]["coreApi"]),
                         {"docs/RULE.md", "CONTEXT_REVISION"})
        self.assertEqual(git(clone, "rev-parse", "origin/" + publisher.BRANCH),
                         result["heads"]["coreApi"])

    def conflicting_branches(self, filename, *, delete_on_main=False):
        root = self.roots["coreApi"]
        (root / filename).write_text("original\n")
        self.commit(root, "shared base")
        git(root, "switch", "-c", "merge-candidate")
        (root / filename).write_text("candidate\n")
        self.commit(root, "candidate version")
        candidate = git(root, "rev-parse", "HEAD")
        git(root, "switch", "main")
        if delete_on_main:
            (root / filename).unlink()
        else:
            (root / filename).write_text("main version\n")
        self.commit(root, "main version")
        git(root, "switch", "merge-candidate")
        return root, candidate

    def test_snapshot_removed_on_main_is_not_resurrected_by_merge(self):
        root, candidate = self.conflicting_branches("docs/RULE.md", delete_on_main=True)
        publisher.merge_snapshot_main(root, {"docs/RULE.md"})
        self.assertFalse((root / "docs/RULE.md").exists())
        self.assertEqual(git(root, "ls-files", "--unmerged"), "")
        git(root, "merge-base", "--is-ancestor", candidate, "HEAD")
        git(root, "merge-base", "--is-ancestor", "origin/main", "HEAD")

    def test_conflict_outside_snapshot_allowlist_fails_without_push(self):
        root, candidate = self.conflicting_branches("business.py")
        with self.assertRaisesRegex(ContractSyncError, "non-snapshot conflicts"):
            publisher.merge_snapshot_main(root, {"docs/RULE.md"})
        self.assertEqual(git(root, "rev-parse", "HEAD"), candidate)
        self.assertEqual(git(root, "rev-parse", "origin/merge-candidate"), candidate)
        self.assertTrue(git(root, "ls-files", "--unmerged"))

    def test_merge_error_without_conflicts_is_not_swallowed(self):
        original_run = publisher.run
        def fail_merge(*args, **kwargs):
            if "merge" in args:
                raise ContractSyncError("merge failed without conflicts")
            return original_run(*args, **kwargs)
        with patch.object(publisher, "run", side_effect=fail_merge):
            with self.assertRaisesRegex(ContractSyncError, "merge failed without conflicts"):
                publisher.merge_snapshot_main(self.roots["coreApi"], {"docs/RULE.md"})

    def test_remote_main_move_during_publishing_aborts_without_pr_writes(self):
        original = publisher.assert_remote_main
        checks = []
        def check(root, expected):
            checks.append(root)
            if root == self.roots["coreApi"]:
                raise ContractSyncError("main changed during propagation")
            original(root, expected)
        with patch.object(publisher, "assert_remote_main", side_effect=check):
            with self.assertRaisesRegex(ContractSyncError, "main changed"):
                self.publish()
        self.assertEqual(self.bodies, [])

    def test_changed_pr_head_prevents_body_update(self):
        calls = []
        def cli(*args, **kwargs):
            calls.append(args)
            if args[:3] == ("gh", "pr", "list"):
                return json.dumps([{"number": 7}])
            if args[:2] == ("gh", "api"):
                return json.dumps({"head": {"sha": "c" * 40, "repo": {"full_name": "example/core-api"}}})
            self.fail(f"Must not write PR: {args}")
        # render_body needs the complete tuple; only the API head is changed.
        from test_contract_evidence import candidate
        result = candidate()
        with patch.object(publisher, "run", side_effect=cli):
            with self.assertRaisesRegex(ContractSyncError, "head changed"):
                publisher.upsert_pr(self.roots["coreApi"], "example", "coreApi", result)
        self.assertFalse(any("edit" in call or "create" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
