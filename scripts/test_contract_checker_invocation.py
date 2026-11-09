"""Run the actual Analytics PowerShell invocation against an isolated Python checker."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def analytics_block():
    lines = (ROOT / "scripts/verify-contract-sync.ps1").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("$analyticsContractVerifier ="))
    end = next(i for i in range(start, len(lines)) if lines[i].startswith(
        "Write-Host 'Trading Analytics create 202, replay 200, and conflict 409 contract verified.'"))
    return "\n".join(lines[start:end + 1])


class ContractCheckerInvocationTest(unittest.TestCase):
    def execute(self, fail):
        shell = shutil.which("pwsh") or shutil.which("powershell.exe")
        self.assertIsNotNone(shell, "PowerShell is required to execute the real checker invocation")
        with tempfile.TemporaryDirectory(prefix="contract checker regression ") as temporary:
            root = Path(temporary)
            scripts = root / "context/scripts"
            scripts.mkdir(parents=True)
            (scripts / "checker_helper.py").write_text("VALUE = 'IMPORTED_CHECKER_HELPER'\n", encoding="utf-8")
            (scripts / "verify-trading-analytics-contract.py").write_text(
                "import checker_helper\nprint(checker_helper.VALUE)\n" +
                ("raise SystemExit(23)\n" if fail else ""), encoding="utf-8")
            wrapper = root / "invoke.ps1"
            wrapper.write_text(
                "param([string]$ContextPath)\n$ErrorActionPreference = 'Stop'\n" + analytics_block() + "\n",
                encoding="utf-8")

            def manifest():
                return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in root.rglob("*") if p.is_file()}

            before = manifest()
            environment = dict(os.environ)
            # The real invocation must protect the checkout even without an ambient no-bytecode setting.
            for key in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONPATH"):
                environment.pop(key, None)
            result = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                 str(wrapper), "-ContextPath", str(scripts.parent)], cwd=root, env=environment,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
            return result, before, manifest()

    def test_successful_checker_import_leaves_files_unchanged(self):
        result, before, after = self.execute(False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("IMPORTED_CHECKER_HELPER", result.stdout)
        self.assertEqual(after, before, "Contract verification must not create bytecode in the checkout")

    def test_checker_failure_propagates_without_creating_files(self):
        result, before, after = self.execute(True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Context Analytics create/replay contract verification failed", result.stdout + result.stderr)
        self.assertEqual(after, before, "A failed checker must also leave the checkout unchanged")


if __name__ == "__main__":
    unittest.main()
