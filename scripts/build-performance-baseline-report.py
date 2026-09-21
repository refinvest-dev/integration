"""Aggregate version 1 Compute benchmark reports into local JSON and Markdown."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from performance_baseline import (
    STAGES,
    BaselineError,
    expected_by_id,
    fixture_identity,
    read_object,
    scenario_definitions,
    statistics,
)


def build_baseline(
    *,
    protocol: dict[str, Any],
    expected: dict[str, Any],
    manifest: dict[str, Any],
    scenarios: dict[str, dict[str, Any]],
    reports_directory: Path,
    environment: dict[str, Any],
    revisions: dict[str, Any],
    official: bool,
    effective: dict[str, Any],
    baseline_id: str,
    generated_at: str,
) -> dict[str, Any]:
    expected_map = expected_by_id(expected)
    if set(expected_map) != set(scenarios):
        raise BaselineError("Expected results and scenario definitions differ.")
    fixture = fixture_identity(manifest)
    aggregates: list[dict[str, Any]] = []
    correctness_failures: list[dict[str, Any]] = []
    for scenario_id in sorted(scenarios):
        batches: list[dict[str, Any]] = []
        all_samples: list[dict[str, Any]] = []
        for batch in range(1, effective["batches"] + 1):
            path = reports_directory / f"{scenario_id}-batch-{batch}.json"
            report = read_object(path)
            _validate_identity(report, fixture, scenarios[scenario_id], effective)
            samples = report.get("samples")
            if not isinstance(samples, list) or not samples:
                raise BaselineError(f"Empty runner report: {path.name}")
            copied = []
            for sample in samples:
                if not isinstance(sample, dict):
                    raise BaselineError(f"Invalid sample: {path.name}")
                copied_sample = dict(sample)
                copied_sample["batch"] = batch
                copied.append(copied_sample)
            all_samples.extend(copied)
            successful = [sample for sample in copied if sample.get("status") == "SUCCEEDED"]
            batch_summary = {
                "batch": batch,
                "reportFile": path.name,
                "sampleCount": len(copied),
                "successfulIterations": len(successful),
                "failedIterations": len(copied) - len(successful),
                "total": statistics([sample["totalNanoseconds"] for sample in successful])
                if successful
                else None,
                "correctness": report.get("correctness"),
            }
            batches.append(batch_summary)
            correctness = report.get("correctness")
            if not isinstance(correctness, dict) or correctness.get("passed") is not True:
                correctness_failures.append(
                    {"scenarioId": scenario_id, "batch": batch, "reason": "runner correctness failed"}
                )
        successful = [sample for sample in all_samples if sample.get("status") == "SUCCEEDED"]
        failed = [sample for sample in all_samples if sample.get("status") != "SUCCEEDED"]
        expected_item = expected_map[scenario_id]
        for batch in batches:
            actual = batch["correctness"]
            for field in ("terminalStatus", "tradeCount", "sampleSizeWarning", "resultHash"):
                if not isinstance(actual, dict) or actual.get(field) != expected_item.get(field):
                    correctness_failures.append(
                        {"scenarioId": scenario_id, "batch": batch["batch"], "field": field}
                    )
        stage_summary: dict[str, Any] = {}
        for stage in STAGES:
            values = [
                sample["stageNanoseconds"][stage]
                for sample in successful
                if isinstance(sample.get("stageNanoseconds"), dict)
                and stage in sample["stageNanoseconds"]
            ]
            stage_summary[stage] = statistics(values) if values else None
        medians = [batch["total"]["medianNanoseconds"] for batch in batches if batch["total"]]
        aggregates.append(
            {
                "id": scenario_id,
                "version": scenarios[scenario_id]["scenarioVersion"],
                "batchCount": len(batches),
                "sampleCount": len(all_samples),
                "successfulIterations": len(successful),
                "failedIterations": len(failed),
                "summary": {
                    "total": statistics([sample["totalNanoseconds"] for sample in successful])
                    if successful
                    else None,
                    "stages": stage_summary,
                    "pythonPeakAllocatedBytes": {
                        "median": _integer_median(
                            [sample["pythonPeakAllocatedBytes"] for sample in successful]
                        ),
                        "max": max(sample["pythonPeakAllocatedBytes"] for sample in successful),
                    }
                    if successful
                    else None,
                    "batchMedianSpreadNanoseconds": max(medians) - min(medians) if medians else None,
                },
                "batches": batches,
                "rawSamples": all_samples,
                "correctness": {
                    "passed": not any(
                        failure["scenarioId"] == scenario_id for failure in correctness_failures
                    ),
                    **{field: expected_item.get(field) for field in (
                        "terminalStatus", "tradeCount", "sampleSizeWarning", "resultHash"
                    )},
                },
            }
        )
    passed = not correctness_failures and all(item["failedIterations"] == 0 for item in aggregates)
    return {
        "baselineVersion": 1,
        "baselineId": baseline_id,
        "generatedAt": generated_at,
        "official": official,
        "passed": passed,
        "protocol": {
            "protocolVersion": protocol["protocolVersion"],
            "canonicalizationVersion": protocol["canonicalizationVersion"],
            "cacheMode": effective["cacheMode"],
            "warmupsPerBatch": effective["warmupsPerBatch"],
            "iterationsPerBatch": effective["iterationsPerBatch"],
            "batches": effective["batches"],
            "p95Method": protocol["p95Method"],
            "failurePolicy": protocol["failurePolicy"],
            "environmentPolicy": protocol["environmentPolicy"],
            "executionOrder": protocol["executionOrder"],
        },
        "fixture": fixture,
        "revisions": revisions,
        "environment": environment,
        "scenarios": aggregates,
        "correctness": {"passed": not correctness_failures, "failures": correctness_failures},
    }


def _validate_identity(
    report: dict[str, Any], fixture: dict[str, Any], scenario: dict[str, Any], effective: dict[str, Any]
) -> None:
    if report.get("reportVersion") != 1 or report.get("canonicalizationVersion") != 1:
        raise BaselineError("Unsupported runner report version.")
    if report.get("fixture") != fixture:
        raise BaselineError("Runner fixture identity mismatch.")
    expected_scenario = {"id": scenario["scenarioId"], "version": scenario["scenarioVersion"]}
    if report.get("scenario") != expected_scenario:
        raise BaselineError("Runner scenario identity mismatch.")
    execution = report.get("execution")
    if not isinstance(execution, dict) or any(
        execution.get(field) != value
        for field, value in {
            "cacheMode": effective["cacheMode"],
            "warmups": effective["warmupsPerBatch"],
            "iterations": effective["iterationsPerBatch"],
        }.items()
    ):
        raise BaselineError("Runner execution protocol mismatch.")


def _integer_median(values: list[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) // 2


def _duration(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value / 1_000_000:.3f} ms"


def markdown(baseline: dict[str, Any]) -> str:
    protocol = baseline["protocol"]
    lines = [
        f"# Compute performance baseline `{baseline['baselineId']}`",
        "",
        f"- Generated: {baseline['generatedAt']}",
        f"- Mode: {'official' if baseline['official'] else 'smoke'}",
        f"- Passed: {str(baseline['passed']).lower()}",
        f"- Fixture: `{baseline['fixture']['id']}` v{baseline['fixture']['version']} (`{baseline['fixture']['manifestSha256']}`)",
        f"- Compute revision: `{baseline['revisions'].get('compute')}`",
        f"- Engine/Python: `{baseline['environment'].get('engineVersion')}` / `{baseline['environment'].get('pythonVersion')}`",
        f"- Protocol: warmups={protocol['warmupsPerBatch']}, iterations={protocol['iterationsPerBatch']}, batches={protocol['batches']}, cache={protocol['cacheMode']}, p95={protocol['p95Method']}",
        "",
        "This is a same-machine comparison baseline, not a production SLO or user-traffic measurement. No performance threshold is established.",
        "",
        "| Scenario | Samples | Total median | Total p95 | Python peak max | Batch median spread | Correctness |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in baseline["scenarios"]:
        total = item["summary"]["total"] or {}
        peak = item["summary"]["pythonPeakAllocatedBytes"] or {}
        lines.append(
            f"| {item['id']} | {item['sampleCount']} | {_duration(total.get('medianNanoseconds'))} | "
            f"{_duration(total.get('p95Nanoseconds'))} | {peak.get('max', '—')} B | "
            f"{_duration(item['summary']['batchMedianSpreadNanoseconds'])} | "
            f"{'PASS' if item['correctness']['passed'] else 'FAIL'} |"
        )
    lines.extend(["", "## Stage summaries", ""])
    for item in baseline["scenarios"]:
        lines.extend([
            f"### {item['id']}", "",
            "| Stage | Median | p95 |", "|---|---:|---:|",
        ])
        for stage, summary in item["summary"]["stages"].items():
            summary = summary or {}
            lines.append(
                f"| {stage} | {_duration(summary.get('medianNanoseconds'))} | {_duration(summary.get('p95Nanoseconds'))} |"
            )
        lines.append("")
    failures = baseline["correctness"]["failures"]
    lines.extend(["## Failures or omissions", "", "None." if not failures else f"{len(failures)} correctness failure(s); inspect baseline JSON.", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--expected-results", type=Path, required=True)
    parser.add_argument("--fixture-manifest", type=Path, required=True)
    parser.add_argument("--scenario-directory", type=Path, required=True)
    parser.add_argument("--reports-directory", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--revisions", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    parser.add_argument("--baseline-id", required=True)
    parser.add_argument("--official", action="store_true")
    parser.add_argument("--cache-mode", required=True)
    parser.add_argument("--warmups", type=int, required=True)
    parser.add_argument("--iterations", type=int, required=True)
    parser.add_argument("--batches", type=int, required=True)
    args = parser.parse_args()
    try:
        baseline = build_baseline(
            protocol=read_object(args.protocol), expected=read_object(args.expected_results),
            manifest=read_object(args.fixture_manifest), scenarios=scenario_definitions(args.scenario_directory),
            reports_directory=args.reports_directory, environment=read_object(args.environment),
            revisions=read_object(args.revisions), official=args.official,
            effective={"cacheMode": args.cache_mode, "warmupsPerBatch": args.warmups,
                       "iterationsPerBatch": args.iterations, "batches": args.batches},
            baseline_id=args.baseline_id, generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        args.output_json.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        args.output_markdown.write_text(markdown(baseline), encoding="utf-8")
    except BaselineError as error:
        parser.exit(2, f"baseline builder: {error}\n")
    return 0 if baseline["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
