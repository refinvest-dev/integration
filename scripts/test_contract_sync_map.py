import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from contract_sync import (
    ContractSyncError, apply_plan, context_files, expand_map, load_map,
    plan, verify_revisions,
)


class ContractSyncMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_map()
        cls.context = Path(__file__).resolve().parents[2] / "context"
        cls.sources = context_files(cls.context, "HEAD")
        cls.replicas = expand_map(cls.manifest, cls.sources)

    def targets(self, source):
        return {
            (replica.repository, replica.target)
            for replica in self.replicas if replica.source == source
        }

    def test_analysis_draft_exact_optional_mapping(self):
        names = {"openapi/drafts/trading-review-analysis-read.yaml", "openapi/drafts/trading-review-analysis.yaml"}
        before = self.sources - names
        self.assertFalse(any(r.source in names for r in expand_map(self.manifest, before)))
        after = expand_map(self.manifest, before | names)
        for name in names:
            with self.subTest(name=name):
                self.assertEqual({(r.repository, r.target, r.mode) for r in after if r.source == name},
                                 {("coreApi", name, "text")})
                self.assertNotIn(name, self.manifest["informationalFiles"])
        for name in ("openapi/drafts/trading-review-analysis-extra.yaml", "openapi/drafts/trading-review-analysis.yaml.backup"):
            with self.subTest(name=name), self.assertRaisesRegex(ContractSyncError, "Unclassified"):
                expand_map(self.manifest, before | names | {name})

    def test_api_and_document_ownership(self):
        self.assertEqual(
            self.targets("openapi/core-api.yaml"),
            {("coreApi", "openapi/core-api.yaml"),
             ("web", "docs/api/open-api.yaml")},
        )
        self.assertEqual(
            self.targets("openapi/compute-api.yaml"),
            {("coreApi", "openapi/compute-api.yaml"),
             ("computeApi", "openapi/compute-api.yaml"),
             ("web", "docs/api/compute-api.yaml")},
        )
        self.assertEqual(
            {replica.repository for replica in self.replicas
             if replica.source == "docs/ROADMAP.md"}, {"coreApi"},
        )
        self.assertEqual(
            {replica.repository for replica in self.replicas
             if replica.source == "fixtures/trading-normalization/result-integrity-v2.json"},
            {"coreApi", "computeApi", "integration"},
        )

    def test_unknown_contract_file_fails_closed(self):
        with self.assertRaisesRegex(ContractSyncError, "Unclassified"):
            expand_map(self.manifest, self.sources | {"fixtures/new-protocol/vector.json"})

    def test_review_drafts_have_core_snapshots_only(self):
        for name in ("trading-review-owner-query.yaml", "trading-review-member-authority.yaml"):
            source = "openapi/drafts/" + name
            with self.subTest(source=source):
                self.assertEqual(self.targets(source), {("coreApi", source)})
                self.assertNotIn(source, self.manifest["informationalFiles"])
        fixture = "fixtures/trading-review-member-authority-draft/authority-cases.json"
        self.assertEqual(self.targets(fixture), {(
            "coreApi",
            "tradingreview/application/src/test/resources/trading-review-member-authority-draft/authority-cases.json",
        )})
        self.assertEqual(
            {replica.mode for replica in self.replicas if replica.source == fixture},
            {"text"},
        )

    def test_new_draft_contract_file_still_fails_closed(self):
        for source in (
            "openapi/drafts/new-service.yaml",
            "fixtures/trading-review-member-authority-draft/new-cases.json",
        ):
            with self.subTest(source=source), self.assertRaisesRegex(ContractSyncError, "Unclassified"):
                expand_map(self.manifest, self.sources | {source})

    def test_account_book_draft_mapping_before_and_after_source_exists(self):
        source = "openapi/drafts/trading-review-account-book.yaml"
        before = self.sources - {source}
        self.assertFalse(any(r.source == source for r in expand_map(self.manifest, before)))
        after = expand_map(self.manifest, before | {source})
        self.assertEqual({(r.repository, r.target, r.mode) for r in after if r.source == source},
                         {("coreApi", source, "text")})
        for unknown in ("openapi/drafts/trading-review-account-book-extra.yaml",
                        source + ".backup"):
            with self.subTest(unknown=unknown), self.assertRaisesRegex(ContractSyncError, "Unclassified"):
                expand_map(self.manifest, before | {unknown})

    def test_import_draft_mapping_before_and_after_source_exists(self):
        source = "openapi/drafts/trading-review-import.yaml"
        before = self.sources - {source}
        self.assertFalse(any(r.source == source for r in expand_map(self.manifest, before)))
        after = expand_map(self.manifest, before | {source})
        self.assertEqual(
            {(r.repository, r.target, r.mode) for r in after if r.source == source},
            {("coreApi", source, "text")},
        )
        self.assertNotIn(source, self.manifest["informationalFiles"])

    def test_import_draft_neighbors_remain_unclassified(self):
        before = self.sources - {"openapi/drafts/trading-review-import.yaml"}
        for unknown in ("openapi/drafts/trading-review-import-extra.yaml",
                        "openapi/drafts/trading-review-import.yaml.backup"):
            with self.subTest(unknown=unknown), self.assertRaisesRegex(ContractSyncError, "Unclassified"):
                expand_map(self.manifest, before | {unknown})

    def test_core_local_document_collision_fails_closed(self):
        with self.assertRaisesRegex(ContractSyncError, "collides"):
            expand_map(self.manifest, self.sources | {"docs/README.md"})

    def test_informational_file_does_not_create_replica(self):
        self.assertFalse(self.targets("AGENTS.md"))
        self.assertFalse(self.targets("fixtures/trading-analytics/verify-v1.mjs"))


class RevisionProvenanceTest(unittest.TestCase):
    def test_docs_only_change_keeps_other_repository_pins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()

            def run(*arguments):
                return subprocess.check_output(
                    ["git", "-C", str(context), *arguments], stderr=subprocess.PIPE
                ).decode().strip()

            run("init", "-q")
            run("config", "user.name", "Contract Test")
            run("config", "user.email", "contract-test@example.invalid")
            (context / "docs").mkdir()
            (context / "openapi").mkdir()
            (context / "docs" / "RULE.md").write_text("first\n")
            (context / "openapi" / "compute-api.yaml").write_text("first\n")
            run("add", ".")
            run("commit", "-qm", "first")
            first = run("rev-parse", "HEAD")
            (context / "docs" / "RULE.md").write_text("second\n")
            run("add", ".")
            run("commit", "-qm", "docs")
            second = run("rev-parse", "HEAD")

            manifest = {
                "schemaVersion": 1,
                "repositories": {
                    name: {"revision": "CONTEXT_REVISION"}
                    for name in ("coreApi", "computeApi", "web", "integration")
                },
                "mappings": [
                    {"sourceDirectory": "docs", "files": "*.md", "mode": "text",
                     "targets": [{"repository": "coreApi", "directory": "docs"}]},
                    {"source": "openapi/compute-api.yaml", "mode": "text",
                     "targets": [{"repository": "coreApi", "path": "openapi/compute-api.yaml"},
                                 {"repository": "computeApi", "path": "openapi/compute-api.yaml"}]},
                ],
                "unmappedContractPrefixes": ["docs/", "openapi/"],
                "informationalFiles": [],
            }
            roots = {}
            for name in manifest["repositories"]:
                roots[name] = root / name
                roots[name].mkdir()
                (roots[name] / "CONTEXT_REVISION").write_text(first + "\n")

            with self.assertRaisesRegex(ContractSyncError, "coreApi.*RULE.md"):
                verify_revisions(manifest, context, second, roots, include_web=True)

            (roots["coreApi"] / "CONTEXT_REVISION").write_text(second + "\n")
            revisions = verify_revisions(manifest, context, second, roots, include_web=True)
            self.assertEqual(revisions["coreApi"], second)
            self.assertEqual(revisions["computeApi"], first)
            self.assertEqual(revisions["web"], first)
            self.assertEqual(revisions["integration"], first)

    def test_plan_and_apply_are_scoped_to_changed_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()

            def run(repository, *arguments):
                return subprocess.check_output(
                    ["git", "-C", str(repository), *arguments],
                    stderr=subprocess.PIPE,
                ).decode().strip()

            def initialize(repository):
                run(repository, "init", "-q")
                run(repository, "config", "user.name", "Contract Test")
                run(repository, "config", "user.email", "contract-test@example.invalid")

            initialize(context)
            (context / "docs").mkdir()
            (context / "docs" / "RULE.md").write_text("first\n")
            run(context, "add", ".")
            run(context, "commit", "-qm", "first")
            first = run(context, "rev-parse", "HEAD")
            manifest = {
                "schemaVersion": 1,
                "repositories": {
                    name: {"revision": "CONTEXT_REVISION"}
                    for name in ("coreApi", "computeApi", "web", "integration")
                },
                "mappings": [
                    {"sourceDirectory": "docs", "files": "*.md", "mode": "text",
                     "targets": [{"repository": "coreApi", "directory": "docs"}]},
                ],
                "unmappedContractPrefixes": ["docs/"],
                "informationalFiles": [],
            }
            roots = {}
            for name in manifest["repositories"]:
                repository = root / name
                repository.mkdir()
                initialize(repository)
                (repository / "CONTEXT_REVISION").write_text(first + "\n")
                if name == "coreApi":
                    (repository / "docs").mkdir()
                    (repository / "docs" / "RULE.md").write_text("first\n")
                run(repository, "add", ".")
                run(repository, "commit", "-qm", "initial snapshot")
                roots[name] = repository

            (context / "docs" / "RULE.md").write_text("second\n")
            run(context, "add", ".")
            run(context, "commit", "-qm", "docs")
            result = plan(manifest, context, "HEAD", roots)
            self.assertEqual(set(result["repositories"]), {"coreApi"})
            changed = apply_plan(manifest, context, roots, result, "coreApi")
            self.assertEqual(changed, ["docs/RULE.md", "CONTEXT_REVISION"])
            self.assertEqual((roots["coreApi"] / "docs" / "RULE.md").read_text(), "second\n")
            run(roots["coreApi"], "add", ".")
            run(roots["coreApi"], "commit", "-qm", "sync")
            self.assertEqual(plan(manifest, context, "HEAD", roots)["repositories"], {})

            (context / "docs" / "RULE.md").unlink()
            run(context, "add", "-A")
            run(context, "commit", "-qm", "remove docs")
            deletion = plan(manifest, context, "HEAD", roots)
            self.assertEqual(deletion["repositories"]["coreApi"][0]["action"], "delete")
            apply_plan(manifest, context, roots, deletion, "coreApi")
            self.assertFalse((roots["coreApi"] / "docs" / "RULE.md").exists())


if __name__ == "__main__":
    unittest.main()
