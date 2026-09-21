"""Shared validation and aggregation helpers for performance baseline tooling."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

STAGES = (
    "snapshot_load",
    "request_strategy_validation",
    "calendar_resolution",
    "signal_evaluation",
    "entry_exit_resolution",
    "trade_realization",
    "performance_benchmark_assembly",
    "result_serialization",
)
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class BaselineError(ValueError):
    pass


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BaselineError(f"Invalid JSON input: {path.name}") from error
    if not isinstance(value, dict):
        raise BaselineError(f"JSON input must be an object: {path.name}")
    return value


def statistics(values: list[int]) -> dict[str, int]:
    if not values or any(type(value) is not int or value < 0 for value in values):
        raise BaselineError("Duration statistics require non-negative integer samples.")
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) // 2
    )
    return {
        "minNanoseconds": ordered[0],
        "maxNanoseconds": ordered[-1],
        "medianNanoseconds": median,
        "p95Nanoseconds": ordered[math.ceil(0.95 * len(ordered)) - 1],
    }


def expected_by_id(expected: dict[str, Any]) -> dict[str, dict[str, Any]]:
    scenarios = expected.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise BaselineError("Expected results must contain a non-empty scenarios array.")
    result: dict[str, dict[str, Any]] = {}
    for item in scenarios:
        if not isinstance(item, dict) or not isinstance(item.get("scenarioId"), str):
            raise BaselineError("Expected scenario entries must have scenarioId.")
        scenario_id = item["scenarioId"]
        if scenario_id in result:
            raise BaselineError(f"Duplicate expected scenario: {scenario_id}")
        result[scenario_id] = item
    return result


def scenario_definitions(directory: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        value = read_object(path)
        scenario_id = value.get("scenarioId")
        if not isinstance(scenario_id, str) or scenario_id in result:
            raise BaselineError("Scenario IDs must be present and unique.")
        result[scenario_id] = value
    if not result:
        raise BaselineError("No scenarios were found.")
    return result


def fixture_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    digest = manifest.get("storageIndependentManifestSha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise BaselineError("Fixture manifest hash is invalid.")
    return {
        "id": manifest.get("fixtureId"),
        "version": manifest.get("fixtureDefinitionVersion"),
        "manifestSha256": f"sha256:{digest}",
    }


def assert_no_sensitive_or_absolute_strings(value: Any, field: str = "root") -> None:
    sensitive = re.compile(
        r"(?i)(password|secret|credential|authorization|bearer\s+|api[_-]?key|token)"
    )
    windows_path = re.compile(r"^[A-Za-z]:[\\/]")
    unix_path = re.compile(r"^/(?:home|Users|workspace|tmp|var|opt)/")
    if isinstance(value, dict):
        for key, item in value.items():
            if sensitive.search(str(key)):
                raise BaselineError(f"Sensitive field is forbidden: {field}.{key}")
            assert_no_sensitive_or_absolute_strings(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_sensitive_or_absolute_strings(item, f"{field}[{index}]")
    elif isinstance(value, str):
        if sensitive.search(value):
            raise BaselineError(f"Sensitive-looking value is forbidden: {field}")
        if windows_path.search(value) or unix_path.search(value):
            raise BaselineError(f"Absolute local path is forbidden: {field}")
