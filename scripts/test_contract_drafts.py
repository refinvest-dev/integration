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


if __name__ == "__main__":
    unittest.main()
