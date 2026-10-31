"""Keep subprocess failure evidence while excluding credential values."""

import json
import os
import subprocess
import time
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch
import pytest

from process_control import run_process_control


def test_failure_preserves_exit_and_redacts_environment_and_nested_config(tmp_path):
    environment = {"POSTGRES_PASSWORD":"database-password", "REFINVEST_INTERNAL_API_KEY":"internal-key",
        "SPRING_APPLICATION_JSON":json.dumps({"refinvest.security.jwt-secret":"jwt-value"})}
    def failed_run(*args, **kwargs):
        kwargs["stdout"].write(b"database-password internal-key")
        kwargs["stderr"].write(b"startup failed: jwt-value")
        return CompletedProcess([], 17)

    with patch("process_control.subprocess.run", side_effect=failed_run):
        result = run_process_control(Path("start-service.ps1"),
            ["-RegistryId","test-run","-Service","core-api"], tmp_path, environment)
    assert result.returncode == 17
    raw = result.diagnostic.read_text(encoding="utf-8")
    assert all(secret not in raw for secret in ("database-password","internal-key","jwt-value"))
    saved = json.loads(raw)
    assert saved["registry"] == "test-run" and saved["service"] == "core-api"
    assert "startup failed" in saved["stderr"] and "<redacted>" in raw
    assert "environment" not in saved and "argv" not in saved


def test_process_start_failure_is_preserved_instead_of_losing_the_cause(tmp_path):
    with patch("process_control.subprocess.run", side_effect=OSError("native start failed")):
        result = run_process_control(Path("service.ps1"), [], tmp_path, {})
    assert result.returncode == -1
    assert json.loads(result.diagnostic.read_text(encoding="utf-8"))["stderr"] == "native start failed"


def test_success_is_recorded_and_remains_success(tmp_path):
    def ready_run(*args, **kwargs):
        kwargs["stdout"].write(b"ready")
        return CompletedProcess([], 0)

    with patch("process_control.subprocess.run", side_effect=ready_run):
        result = run_process_control(Path("service.ps1"), [], tmp_path, {})
    assert result.returncode == 0
    assert json.loads(result.diagnostic.read_text(encoding="utf-8"))["stdout"] == "ready"


@pytest.mark.skipif(os.name != "nt", reason="Windows background handle inheritance regression")
def test_background_child_does_not_keep_startup_capture_waiting(tmp_path):
    script = tmp_path / "background.ps1"
    script.write_text("""$taskChild = Start-Process powershell.exe -ArgumentList @('-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 10') -WindowStyle Hidden -PassThru
Write-Output $taskChild.Id
""", encoding="ascii")
    began = time.monotonic()
    result = run_process_control(script, [], tmp_path)
    elapsed = time.monotonic() - began
    child_id = int(json.loads(result.diagnostic.read_text())["stdout"].strip())
    try:
        assert result.returncode == 0 and elapsed < 8
    finally:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f"Stop-Process -Id {child_id} -Force -ErrorAction SilentlyContinue"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
