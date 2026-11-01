"""Payload-free process-control diagnostics for local E2E orchestration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import quote, urlsplit


@dataclass(frozen=True)
class ProcessControlResult:
    returncode: int
    diagnostic: Path


def _secret_values(environment: Mapping[str, str]) -> set[str]:
    values: set[str] = set()
    sensitive = re.compile(r"password|secret|token|(?:api|payload|identity|masking)[_-]?key", re.I)

    def visit(key: str, value: object) -> None:
        if isinstance(value, str) and value and sensitive.search(key):
            values.add(value)
        if isinstance(value, dict):
            for name, item in value.items():
                visit(str(name), item)

    for key, value in environment.items():
        visit(key, value)
        if value and (key.endswith("DATABASE_URL") or key.endswith("DATASOURCE_URL")):
            values.add(value)
            parsed = urlsplit(value.removeprefix("jdbc:"))
            if parsed.password:
                values.add(parsed.password)
    try:
        visit("config", json.loads(environment.get("SPRING_APPLICATION_JSON", "{}")))
    except (ValueError, TypeError):
        pass
    return {variant for value in values for variant in
        (value, quote(value, safe=""), json.dumps(value, ensure_ascii=True)[1:-1],
         json.dumps(value, ensure_ascii=False)[1:-1]) if variant}


def run_process_control(script: Path, arguments: Sequence[str], run_dir: Path,
                        environment: Mapping[str, str] | None = None) -> ProcessControlResult:
    env = dict(os.environ if environment is None else environment)
    secrets = sorted(_secret_values(env), key=len, reverse=True)

    def redact(value: bytes | str) -> str:
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        for secret in secrets:
            text = text.replace(secret, "<redacted>")
        return text

    directory = run_dir / "process-control"
    directory.mkdir(parents=True, exist_ok=True)
    diagnostic = directory / f"{time.time_ns()}-{script.stem}.json"
    entry: dict[str, object] = {"script": script.name}
    # Do not save complete argv or environment; these may contain credentials.
    for flag, name in (("-RegistryId", "registry"), ("-Service", "service")):
        if flag in arguments:
            index = list(arguments).index(flag)
            if index + 1 < len(arguments):
                entry[name] = arguments[index + 1]
    try:
        # Background JVMs may inherit PowerShell pipe handles. communicate() then waits
        # for JVM shutdown even after the startup script has exited. File capture waits
        # only for the controlling process, while keeping the same redacted diagnostics.
        with tempfile.TemporaryFile(dir=directory) as stdout, tempfile.TemporaryFile(dir=directory) as stderr:
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
                str(script), *arguments], env=env, stdout=stdout, stderr=stderr,
                stdin=subprocess.DEVNULL, check=False)
            code = result.returncode
            stdout.seek(0)
            stderr.seek(0)
            entry.update(returncode=code, stdout=redact(stdout.read()), stderr=redact(stderr.read()))
    except OSError as error:
        code = -1
        entry.update(returncode=code, errorType=type(error).__name__, stderr=redact(str(error)))
    diagnostic.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if code != 0:
        print(f"Process control failed: {script.name}; exit={code}; diagnostic={diagnostic}", flush=True)
    return ProcessControlResult(code, diagnostic)
