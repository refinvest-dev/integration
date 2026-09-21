"""Strict verifier for local version 1 performance baseline reports."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from performance_baseline import (
    STAGES,
    BaselineError,
    assert_no_sensitive_or_absolute_strings,
    expected_by_id,
    fixture_identity,
    read_object,
    scenario_definitions,
    statistics,
)


def verify(
    baseline: dict[str, Any], protocol: dict[str, Any], expected: dict[str, Any],
    manifest: dict[str, Any], definitions: dict[str, dict[str, Any]],
) -> None:
    if baseline.get("baselineVersion") != 1:
        raise BaselineError("Unsupported baselineVersion.")
    report_protocol = baseline.get("protocol")
    if not isinstance(report_protocol, dict) or report_protocol.get("protocolVersion") != 1:
        raise BaselineError("Missing version 1 protocol.")
    if baseline.get("official") is True:
        defaults = {
            "canonicalizationVersion": protocol["canonicalizationVersion"],
            "cacheMode": protocol["cacheModes"][0],
            "warmupsPerBatch": protocol["warmupsPerBatch"],
            "iterationsPerBatch": protocol["iterationsPerBatch"],
            "batches": protocol["batches"],
            "p95Method": protocol["p95Method"],
        }
        if any(report_protocol.get(key) != value for key, value in defaults.items()):
            raise BaselineError("Official baseline does not use protocol defaults.")
    fixture = fixture_identity(manifest)
    if baseline.get("fixture") != fixture or expected.get("fixture") != fixture:
        raise BaselineError("Fixture identity mismatch.")
    expected_map = expected_by_id(expected)
    raw_scenarios = baseline.get("scenarios")
    if not isinstance(raw_scenarios, list):
        raise BaselineError("Baseline scenarios must be an array.")
    ids = [item.get("id") for item in raw_scenarios if isinstance(item, dict)]
    if len(ids) != len(set(ids)) or set(ids) != set(definitions) or set(ids) != set(expected_map):
        raise BaselineError("Scenario list is incomplete or contains duplicates.")
    for item in raw_scenarios:
        if not isinstance(item, dict):
            raise BaselineError("Invalid baseline scenario.")
        _verify_scenario(item, report_protocol, expected_map[item["id"]])
    revisions = baseline.get("revisions")
    environment = baseline.get("environment")
    if not isinstance(revisions, dict) or not revisions.get("integration") or not revisions.get("compute"):
        raise BaselineError("Required revisions are missing.")
    if not isinstance(environment, dict) or not environment.get("pythonVersion") or not environment.get("os"):
        raise BaselineError("Required environment metadata is missing.")
    failures = baseline.get("correctness", {}).get("failures")
    all_passed = all(item.get("correctness", {}).get("passed") is True for item in raw_scenarios)
    no_failed_samples = all(item.get("failedIterations") == 0 for item in raw_scenarios)
    expected_passed = isinstance(failures, list) and not failures and all_passed and no_failed_samples
    if baseline.get("passed") is not expected_passed:
        raise BaselineError("Top-level passed flag is inconsistent.")
    if baseline.get("official") is True and not expected_passed:
        raise BaselineError("Official baseline contains failures.")
    assert_no_sensitive_or_absolute_strings(baseline)


def _verify_scenario(item: dict[str, Any], protocol: dict[str, Any], expected: dict[str, Any]) -> None:
    expected_samples = protocol["iterationsPerBatch"] * protocol["batches"]
    if item.get("batchCount") != protocol["batches"] or len(item.get("batches", [])) != protocol["batches"]:
        raise BaselineError(f"Batch count mismatch: {item.get('id')}")
    samples = item.get("rawSamples")
    if not isinstance(samples, list) or len(samples) != expected_samples or item.get("sampleCount") != expected_samples:
        raise BaselineError(f"Iteration count mismatch: {item.get('id')}")
    correctness = item.get("correctness")
    for field in ("terminalStatus", "tradeCount", "sampleSizeWarning", "resultHash"):
        if not isinstance(correctness, dict) or correctness.get(field) != expected.get(field):
            raise BaselineError(f"Expected result mismatch for {item.get('id')}: {field}")
    successful = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise BaselineError("Invalid raw sample.")
        total = sample.get("totalNanoseconds")
        stages = sample.get("stageNanoseconds")
        if type(total) is not int or total < 0:
            raise BaselineError("Negative or invalid total duration.")
        if sample.get("status") == "SUCCEEDED":
            if not isinstance(stages, dict) or set(stages) != set(STAGES):
                raise BaselineError("Successful sample stage list is incomplete.")
            if any(type(value) is not int or value < 0 for value in stages.values()):
                raise BaselineError("Negative or invalid stage duration.")
            if any(sample.get(field) != expected.get(field) for field in (
                "terminalStatus", "tradeCount", "sampleSizeWarning", "resultHash"
            )):
                raise BaselineError("Raw sample correctness mismatch.")
            successful.append(sample)
    if len(successful) != item.get("successfulIterations"):
        raise BaselineError("Successful iteration count mismatch.")
    if len(samples) - len(successful) != item.get("failedIterations"):
        raise BaselineError("Failed iteration count mismatch.")
    summary = item.get("summary")
    if not isinstance(summary, dict) or not successful:
        raise BaselineError("Scenario summary is missing.")
    if summary.get("total") != statistics([sample["totalNanoseconds"] for sample in successful]):
        raise BaselineError("Total summary mismatch.")
    stage_summary = summary.get("stages")
    if not isinstance(stage_summary, dict) or set(stage_summary) != set(STAGES):
        raise BaselineError("Stage summary list mismatch.")
    for stage in STAGES:
        if stage_summary[stage] != statistics([sample["stageNanoseconds"][stage] for sample in successful]):
            raise BaselineError(f"Stage summary mismatch: {stage}")
    batch_medians = []
    for batch in item["batches"]:
        batch_samples = [sample for sample in successful if sample.get("batch") == batch.get("batch")]
        if len(batch_samples) != protocol["iterationsPerBatch"]:
            raise BaselineError("Batch iteration count mismatch.")
        calculated = statistics([sample["totalNanoseconds"] for sample in batch_samples])
        if batch.get("total") != calculated:
            raise BaselineError("Batch summary mismatch.")
        batch_medians.append(calculated["medianNanoseconds"])
    if summary.get("batchMedianSpreadNanoseconds") != max(batch_medians) - min(batch_medians):
        raise BaselineError("Batch median spread mismatch.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--expected-results", type=Path, required=True)
    parser.add_argument("--fixture-manifest", type=Path, required=True)
    parser.add_argument("--scenario-directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify(read_object(args.baseline), read_object(args.protocol), read_object(args.expected_results),
               read_object(args.fixture_manifest), scenario_definitions(args.scenario_directory))
    except BaselineError as error:
        parser.exit(1, f"baseline verification failed: {error}\n")
    print(f"Verified performance baseline: {args.baseline.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
