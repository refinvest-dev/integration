"""Exercise draft gates through the real PowerShell entrypoint, without services or DB."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


INTEGRATION = Path(__file__).resolve().parents[1]
WORKSPACE = INTEGRATION.parent
CONTEXT = WORKSPACE / "context"
CORE = WORKSPACE / "api/refinvest"
OWNER = "openapi/drafts/trading-review-owner-query.yaml"
AUTHORITY = "openapi/drafts/trading-review-member-authority.yaml"
FIXTURE = "trading-review-member-authority-draft/authority-cases.json"
ACCOUNT_BOOK = "openapi/drafts/trading-review-account-book.yaml"
ACCOUNT_BOOK_CHECK = "scripts/verify-trading-review-account-book-draft.py"
IMPORT = "openapi/drafts/trading-review-import.yaml"
IMPORT_CHECK = "scripts/verify-trading-review-import-draft.py"


class DraftSnapshotGateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="contract draft gates ")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.context = root / "context"
        self.core = root / "core"
        self.shell = shutil.which("powershell.exe") or shutil.which("pwsh")
        self.assertIsNotNone(self.shell, "PowerShell is required for the contract gate")
        files = [
            "AI_AGENT.md", "openapi/core-api.yaml", "openapi/compute-api.yaml",
            OWNER, AUTHORITY, "fixtures/" + FIXTURE,
            "scripts/verify-trading-review-owner-query-draft.py",
            "scripts/verify-trading-review-member-authority-draft.py",
        ] + [path.relative_to(CONTEXT).as_posix() for path in (CONTEXT / "docs").glob("*.md")]
        if (CONTEXT / ACCOUNT_BOOK).exists():
            files += [ACCOUNT_BOOK, ACCOUNT_BOOK_CHECK]
        if (CONTEXT / IMPORT).exists():
            files += [IMPORT, IMPORT_CHECK]
        for name in files:
            destination = self.context / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(CONTEXT / name, destination)
        replicas = [("AI_AGENT.md", "docs/AI_AGENT.md")]
        replicas += [(name, name) for name in files if name.startswith(("docs/", "openapi/"))]
        replicas += [("fixtures/" + FIXTURE, "tradingreview/application/src/test/resources/" + FIXTURE)]
        for source, target in replicas:
            destination = self.core / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.context / source, destination)
        verifier = self.core / "scripts/verify-context-doc-sync.ps1"
        verifier.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CORE / "scripts/verify-context-doc-sync.ps1", verifier)

    def run_gate(self):
        return subprocess.run([
            self.shell, "-NoProfile", "-File", str(INTEGRATION / "scripts/verify-contract-sync.ps1"),
            "-ContextPath", str(self.context), "-CoreApiPath", str(self.core),
            "-ComputeApiPath", str(WORKSPACE / "compute-api/refinvest"),
            "-WebPath", str(WORKSPACE / "web/web"), "-IncludeWeb",
        ], cwd=INTEGRATION, text=True, capture_output=True, encoding="utf-8", errors="replace", timeout=60)

    def rejected_by(self, message):
        # These gates run before provenance/ordinary fixtures. An unrelated later
        # failure must not satisfy the expected rejection.
        result = self.run_gate()
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn(message, output)
        self.assertNotIn("Context revision provenance verification failed", output)

    def test_missing_draft_snapshot_is_rejected(self):
        (self.core / OWNER).unlink()
        self.rejected_by("Contract replica does not exist")

    def test_changed_draft_snapshot_is_rejected(self):
        with (self.core / AUTHORITY).open("a", encoding="utf-8") as handle:
            handle.write("\n# drift\n")
        self.rejected_by("Contract is out of sync")

    def test_synthetic_fixture_drift_is_rejected(self):
        replica = self.core / "tradingreview/application/src/test/resources" / FIXTURE
        replica.write_bytes(replica.read_bytes() + b"\n")
        self.rejected_by("Contract is out of sync")

    def test_synthetic_fixture_accepts_checkout_newlines_only(self):
        source = self.context / "fixtures" / FIXTURE
        replica = self.core / "tradingreview/application/src/test/resources" / FIXTURE
        lf = source.read_bytes().replace(b"\r\n", b"\n")
        source.write_bytes(lf.replace(b"\n", b"\r\n"))
        replica.write_bytes(lf)
        result = self.run_gate()
        output = result.stdout + result.stderr
        self.assertIn("Trading Review draft schemas/fixtures verified", output)
        # The partial temporary checkout intentionally has no revision marker.
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("Context revision provenance verification failed", output)
        self.assertNotIn("out of sync", output)

    def test_matching_snapshots_cannot_enable_owner_query_runtime(self):
        for root in (self.context, self.core):
            path = root / OWNER
            original = path.read_text(encoding="utf-8")
            self.assertIn("x-runtime-enabled: false", original)
            path.write_text(original.replace("x-runtime-enabled: false", "x-runtime-enabled: true", 1), encoding="utf-8")
        self.rejected_by("runtime enablement is outside this draft")

    def test_matching_snapshots_cannot_enable_authority_runtime(self):
        for root in (self.context, self.core):
            path = root / AUTHORITY
            original = path.read_text(encoding="utf-8")
            self.assertIn("x-runtime-enabled: false", original)
            path.write_text(original.replace("x-runtime-enabled: false", "x-runtime-enabled: true", 1), encoding="utf-8")
        self.rejected_by("authority must remain unadopted")


    @unittest.skipUnless((CONTEXT / ACCOUNT_BOOK).exists(), "candidate absent in pinned Context")
    def test_account_book_missing_snapshot_is_rejected(self):
        (self.core / ACCOUNT_BOOK).unlink()
        self.rejected_by("Contract replica does not exist")

    @unittest.skipUnless((CONTEXT / ACCOUNT_BOOK).exists(), "candidate absent in pinned Context")
    def test_account_book_missing_checker_is_rejected(self):
        (self.context / ACCOUNT_BOOK_CHECK).unlink()
        self.rejected_by("Missing Context Account/Book draft verifier")

    @unittest.skipUnless((CONTEXT / ACCOUNT_BOOK).exists(), "candidate absent in pinned Context")
    def test_matching_account_book_runtime_activation_is_rejected(self):
        for root in (self.context, self.core):
            path = root / ACCOUNT_BOOK
            original = path.read_text(encoding="utf-8")
            self.assertIn("x-runtime-enabled: false", original)
            path.write_text(original.replace("x-runtime-enabled: false", "x-runtime-enabled: true", 1), encoding="utf-8")
        self.rejected_by("runtime enablement forbidden")

    def import_gate_fixture(self, *, source=True, replica=True, checker=True):
        # Exercise the real PowerShell wiring without reimplementing Context's
        # product/schema verifier. All fixture files are isolated temporary paths.
        for root, name in ((self.context, IMPORT), (self.core, IMPORT),
                           (self.context, IMPORT_CHECK)):
            (root / name).unlink(missing_ok=True)
        for root, enabled in ((self.context, source), (self.core, replica)):
            if enabled:
                path = root / IMPORT
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("openapi: 3.0.3\n", encoding="utf-8")
        if checker:
            path = self.context / IMPORT_CHECK
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('print("IMPORT_CHECKER_EXECUTED")\nraise SystemExit(23)\n', encoding="utf-8")

    def test_import_missing_snapshot_is_rejected(self):
        self.import_gate_fixture(replica=False)
        self.rejected_by("Contract replica does not exist")

    def test_import_checker_without_source_is_rejected(self):
        self.import_gate_fixture(source=False)
        self.rejected_by("Contract source does not exist")

    def test_import_source_without_checker_is_rejected(self):
        self.import_gate_fixture(checker=False)
        self.rejected_by("Missing Context Import draft verifier")

    def test_import_checker_failure_is_propagated(self):
        self.import_gate_fixture()
        result = self.run_gate()
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("IMPORT_CHECKER_EXECUTED", output)
        self.assertIn("Import draft verification failed", output)
        self.assertNotIn("Context revision provenance verification failed", output)


    def analysis_gate_fixture(self, *, source_count=2, replica_count=2, checker=True, exit_code=23):
        names = ("openapi/drafts/trading-review-analysis-read.yaml", "openapi/drafts/trading-review-analysis.yaml")
        verifier = "scripts/verify-trading-review-analysis-draft.py"
        for root in (self.context, self.core):
            for name in (*names, verifier):
                (root / name).unlink(missing_ok=True)
        for root, count in ((self.context, source_count), (self.core, replica_count)):
            for name in names[:count]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("openapi: 3.0.3\n", encoding="utf-8")
        if checker:
            path = self.context / verifier
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'print("ANALYSIS_CHECKER_EXECUTED")\nraise SystemExit({exit_code})\n', encoding="utf-8")

    def test_analysis_old_context_remains_optional(self):
        self.analysis_gate_fixture(source_count=0, replica_count=0, checker=False)
        result = self.run_gate()
        output = result.stdout + result.stderr
        self.assertIn("Trading Review draft OpenAPI snapshots are synchronized", output)
        self.assertNotIn("ANALYSIS_CHECKER_EXECUTED", output)

    def test_analysis_checker_without_pair_is_rejected(self):
        self.analysis_gate_fixture(source_count=0)
        self.rejected_by("Analysis draft contract pair incomplete")

    def test_analysis_partial_pair_is_rejected(self):
        self.analysis_gate_fixture(source_count=1)
        self.rejected_by("Analysis draft contract pair incomplete")

    def test_analysis_source_without_checker_is_rejected(self):
        self.analysis_gate_fixture(checker=False)
        self.rejected_by("Missing Context Analysis draft verifier")

    def test_analysis_missing_snapshot_is_rejected(self):
        self.analysis_gate_fixture(replica_count=1)
        self.rejected_by("Contract replica does not exist")

    def test_analysis_snapshot_mismatch_is_rejected(self):
        self.analysis_gate_fixture()
        (self.core / "openapi/drafts/trading-review-analysis.yaml").write_text("different\n", encoding="utf-8")
        self.rejected_by("Contract is out of sync")

    def test_analysis_checker_failure_is_propagated(self):
        self.analysis_gate_fixture()
        result = self.run_gate()
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("ANALYSIS_CHECKER_EXECUTED", output)
        self.assertIn("Analysis draft verification failed", output)
        self.assertNotIn("Context revision provenance verification failed", output)

    def test_analysis_complete_pair_reaches_following_gates(self):
        self.analysis_gate_fixture(exit_code=0)
        path = self.core / "openapi/drafts/trading-review-analysis.yaml"
        path.write_bytes(b"openapi: 3.0.3\r\n")
        result = self.run_gate()
        output = result.stdout + result.stderr
        self.assertIn("ANALYSIS_CHECKER_EXECUTED", output)
        self.assertIn("Analysis candidates checked; no runtime routes, views or scopes enabled.", output)
        self.assertNotIn("Analysis draft verification failed", output)



if __name__ == "__main__":
    unittest.main()
