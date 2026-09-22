"""Validate two official Compute baselines and report a provisional comparison."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from performance_baseline import (
    STAGES, BaselineError, assert_no_sensitive_or_absolute_strings,
    read_object, scenario_definitions,
)

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = Path(__file__).with_name("verify-performance-baseline.py")
spec = importlib.util.spec_from_file_location("baseline_verifier", VERIFIER)
assert spec and spec.loader
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
ENVIRONMENT_FIELDS = (
    "environmentId", "storageProfile", "resourceProfile", "os", "platform", "machine", "cpu",
    "logicalCpuCount", "memoryBytes", "pythonVersion", "engineVersion",
    "computePackageVersion", "dependencyLockSha256",
)
PROTOCOL_FIELDS = (
    "protocolVersion", "canonicalizationVersion", "cacheMode", "warmupsPerBatch",
    "iterationsPerBatch", "batches", "p95Method", "failurePolicy",
    "environmentPolicy", "executionOrder",
)


def _safe_identity(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value))


def validate_report(report: dict[str, Any], protocol: dict[str, Any], expected: dict[str, Any],
                    manifest: dict[str, Any], definitions: dict[str, dict[str, Any]]) -> None:
    verifier.verify(report, protocol, expected, manifest, definitions)
    if report.get("correctness", {}).get("passed") is not True or report.get("passed") is not True:
        raise BaselineError("Report correctness failed.")
    if not _safe_identity(report.get("baselineId")):
        raise BaselineError("Missing or unsafe baseline ID.")
    try:
        timestamp = datetime.fromisoformat(report["generatedAt"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise BaselineError("Missing or invalid execution timestamp.") from error
    if not _safe_identity(report["revisions"].get("compute")):
        raise BaselineError("Missing or unsafe Compute revision.")
    for scenario in report["scenarios"]:
        if scenario.get("version") != definitions[scenario["id"]].get("scenarioVersion"):
            raise BaselineError("Scenario version mismatch.")
        samples = scenario["rawSamples"]
        if any(sample.get("status") != "SUCCEEDED" for sample in samples):
            raise BaselineError("Failed or timed-out iteration.")
        peaks = [sample.get("pythonPeakAllocatedBytes") for sample in samples]
        if any(type(value) is not int or value < 0 for value in peaks):
            raise BaselineError("Missing or invalid peak memory sample.")
        ordered = sorted(peaks)
        middle = len(ordered) // 2
        median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) // 2
        if scenario["summary"].get("pythonPeakAllocatedBytes") != {"median": median, "max": max(peaks)}:
            raise BaselineError("Peak memory summary mismatch.")
        if scenario.get("failedIterations") != 0 or scenario.get("successfulIterations") != len(samples):
            raise BaselineError("Failed or missing iteration.")


def _difference(base: int, candidate: int, unit: str) -> dict[str, Any]:
    return {
        "baseline": base, "candidate": candidate, "absoluteDifference": candidate - base,
        "percentChange": (candidate - base) * 100 / base if base else None,
        "unit": unit,
    }


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    reasons = []
    if baseline.get("official") is not True or candidate.get("official") is not True:
        reasons.append("Both reports must be official.")
    for field in PROTOCOL_FIELDS:
        if baseline["protocol"].get(field) != candidate["protocol"].get(field):
            reasons.append(f"Protocol differs: {field}.")
    if baseline["fixture"] != candidate["fixture"]:
        reasons.append("Fixture identity differs.")
    base_scenarios = {item["id"]: item for item in baseline["scenarios"]}
    candidate_scenarios = {item["id"]: item for item in candidate["scenarios"]}
    if set(base_scenarios) != set(candidate_scenarios):
        reasons.append("Scenario set differs.")
    for scenario_id in sorted(set(base_scenarios) & set(candidate_scenarios)):
        left, right = base_scenarios[scenario_id], candidate_scenarios[scenario_id]
        if left["version"] != right["version"]:
            reasons.append(f"Scenario version differs: {scenario_id}.")
        if left["correctness"] != right["correctness"]:
            reasons.append(f"Expected correctness differs: {scenario_id}.")
    for field in ENVIRONMENT_FIELDS:
        left = baseline["environment"].get(field)
        right = candidate["environment"].get(field)
        if left is None or right is None or left == "" or right == "" or left == {} or right == {}:
            reasons.append(f"Environment metadata missing: {field}.")
        elif left != right:
            reasons.append(f"Environment differs: {field}.")
    if reasons:
        return reasons, [], []
    comparisons = []
    warnings = []
    for scenario_id in sorted(base_scenarios):
        left, right = base_scenarios[scenario_id], candidate_scenarios[scenario_id]
        a, b = left["summary"], right["summary"]
        metrics = {
            "totalMedian": _difference(a["total"]["medianNanoseconds"], b["total"]["medianNanoseconds"], "ns"),
            "totalP95": _difference(a["total"]["p95Nanoseconds"], b["total"]["p95Nanoseconds"], "ns"),
            "peakMemoryMedian": _difference(a["pythonPeakAllocatedBytes"]["median"], b["pythonPeakAllocatedBytes"]["median"], "bytes"),
            "peakMemoryMax": _difference(a["pythonPeakAllocatedBytes"]["max"], b["pythonPeakAllocatedBytes"]["max"], "bytes"),
            "batchMedianSpread": _difference(a["batchMedianSpreadNanoseconds"], b["batchMedianSpreadNanoseconds"], "ns"),
            "sampleCount": _difference(left["sampleCount"], right["sampleCount"], "samples"),
            "failedIterations": _difference(left["failedIterations"], right["failedIterations"], "samples"),
        }
        for stage in STAGES:
            for stat, suffix in (("medianNanoseconds", "Median"), ("p95Nanoseconds", "P95")):
                metrics[f"stage.{stage}.{suffix}"] = _difference(a["stages"][stage][stat], b["stages"][stage][stat], "ns")
        for key in ("totalMedian", "totalP95"):
            change = metrics[key]["percentChange"]
            if change is not None and change >= 20:
                warnings.append({"scenarioId": scenario_id, "metric": key, "percentChange": change,
                                 "reason": "Single-run slowdown of at least 20%; repeat under matched conditions."})
        comparisons.append({"scenarioId": scenario_id, "metrics": metrics,
                            "rawSummary": {"baseline": a, "candidate": b},
                            "correctness": {"baseline": left["correctness"], "candidate": right["correctness"]},
                            "batchMedians": {
                                "baseline": [batch["total"]["medianNanoseconds"] for batch in left["batches"]],
                                "candidate": [batch["total"]["medianNanoseconds"] for batch in right["batches"]],
                                "unit": "ns"}})
    return [], comparisons, warnings


def _report_metadata(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {key: report.get(key) for key in ("baselineId", "generatedAt", "official", "fixture", "protocol", "revisions", "environment", "correctness")}


def make_result(baseline_path: Path, candidate_path: Path, protocol_path: Path,
                expected_path: Path, manifest_path: Path, scenario_directory: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "comparisonVersion": 1, "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "VALIDATION_FAILED", "measurementBoundary": "Compute runner total and non-additive stages; fixture preparation, queue wait, Core HTTP and worker dispatch excluded",
        "percentileMethod": "nearest-rank p95; integer median", "baseline": None, "candidate": None,
        "validation": {"baseline": "NOT_RUN", "candidate": "NOT_RUN", "errors": [], "reportedCorrectness": {}},
        "comparabilityReasons": [], "scenarios": [], "warnings": [],
        "runtimeLoadTest": "Separate artifact; steady/burst percentiles are not part of this comparison.",
    }
    try:
        protocol, expected, manifest = (read_object(path) for path in (protocol_path, expected_path, manifest_path))
        definitions = scenario_definitions(scenario_directory)
    except BaselineError as error:
        result["validation"]["errors"].append({"role": "reference", "reason": str(error)})
        return result
    reports = {}
    for role, path in (("baseline", baseline_path), ("candidate", candidate_path)):
        try:
            report = read_object(path)
            correctness = report.get("correctness")
            result["validation"]["reportedCorrectness"][role] = correctness.get("passed") is True if isinstance(correctness, dict) else None
            validate_report(report, protocol, expected, manifest, definitions)
            reports[role] = report
            result[role] = _report_metadata(report)
            result["validation"][role] = "PASS"
        except (BaselineError, KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
            result["validation"][role] = "FAIL"
            result["validation"]["errors"].append({"role": role, "reason": str(error) if isinstance(error, BaselineError) else "Malformed report structure."})
    if result["validation"]["errors"]:
        return result
    reasons, scenarios, warnings = compare(reports["baseline"], reports["candidate"])
    result["comparabilityReasons"] = reasons
    result["scenarios"] = scenarios
    result["warnings"] = warnings
    result["status"] = "NOT_COMPARABLE" if reasons else "COMPARABLE"
    return result


def markdown(result: dict[str, Any]) -> str:
    lines = ["# Compute benchmark comparison", "", f"- Status: **{result['status']}**",
             f"- Generated: {result['generatedAt']}",
             "- Boundary: Compute runner total and individual stages; fixture preparation, queue wait and Core HTTP excluded.",
             "- Durations: milliseconds for display; JSON contains nanoseconds. Peak memory: Python allocation bytes.",
             "- p95: nearest rank. Stage durations are not additive.", ""]
    for role in ("baseline", "candidate"):
        report = result[role]
        lines.append(f"## {role.title()}")
        lines.append("")
        lines.append(f"- Verification: {result['validation'][role]}")
        if report:
            lines.extend([f"- ID / time: `{report.get('baselineId')}` / {report.get('generatedAt')}",
                          f"- Compute revision: `{(report.get('revisions') or {}).get('compute')}`",
                          f"- Fixture: `{json.dumps(report.get('fixture'), sort_keys=True)}`",
                          f"- Protocol: `{json.dumps(report.get('protocol'), sort_keys=True)}`",
                          f"- Environment: `{json.dumps(report.get('environment'), sort_keys=True)}`",
                          f"- Correctness: `{json.dumps(report.get('correctness'), sort_keys=True)}`"])
        lines.append("")
    if result["validation"]["errors"]:
        lines.extend(["## Validation errors", ""])
        lines.extend(f"- {item['role']}: {item['reason']}" for item in result["validation"]["errors"])
        lines.append("")
    if result["comparabilityReasons"]:
        lines.extend(["## Comparability reasons", ""])
        lines.extend(f"- {reason}" for reason in result["comparabilityReasons"])
        lines.append("")
    for scenario in result["scenarios"]:
        lines.extend([f"## {scenario['scenarioId']}", "",
                      "| Metric | Baseline | Candidate | Difference | Change |",
                      "|---|---:|---:|---:|---:|"])
        for key, metric in scenario["metrics"].items():
            scale = 1_000_000 if metric["unit"] == "ns" else 1
            unit = "ms" if scale != 1 else metric["unit"]
            percent = "N/A (zero baseline)" if metric["percentChange"] is None else f"{metric['percentChange']:+.2f}%"
            lines.append(f"| {key} ({unit}) | {metric['baseline']/scale:.3f} | {metric['candidate']/scale:.3f} | {metric['absoluteDifference']/scale:+.3f} | {percent} |")
        batches = scenario["batchMedians"]
        lines.extend(["", f"Batch total medians (ms): baseline {[round(x/1_000_000, 3) for x in batches['baseline']]}; candidate {[round(x/1_000_000, 3) for x in batches['candidate']]}", ""])
    if result["warnings"]:
        lines.extend(["## Provisional performance warnings", "",
                      "Single-run signals require repeated measurements on a matched machine; they do not fail CI.", ""])
        lines.extend(f"- {item['scenarioId']} {item['metric']}: {item['percentChange']:+.2f}%" for item in result["warnings"])
        lines.append("")
    lines.extend(["## Runtime load tests", "", result["runtimeLoadTest"], ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, default=ROOT / "performance/baseline-protocol.json")
    parser.add_argument("--expected-results", type=Path, default=ROOT / "performance/expected-results.json")
    parser.add_argument("--fixture-manifest", type=Path, default=ROOT / "performance/fixture-manifest.json")
    parser.add_argument("--scenario-directory", type=Path, default=ROOT / "performance/scenarios")
    parser.add_argument("--output-root", type=Path, default=ROOT / ".integration-data/performance/comparisons")
    args = parser.parse_args()
    result = make_result(args.baseline, args.candidate, args.protocol, args.expected_results,
                         args.fixture_manifest, args.scenario_directory)
    try:
        assert_no_sensitive_or_absolute_strings(result)
    except BaselineError:
        result = {"comparisonVersion": 1, "status": "VALIDATION_FAILED", "validation": {
            "baseline": "FAIL", "candidate": "FAIL", "errors": [{"role": "output", "reason": "Unsafe metadata in input report."}]},
                  "baseline": None, "candidate": None, "comparabilityReasons": [], "scenarios": [], "warnings": [],
                  "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                  "runtimeLoadTest": "Separate artifact."}
    output = args.output_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = 1
    while output.exists():
        output = args.output_root / f"{output.name}-{suffix}"
        suffix += 1
    output.mkdir(parents=True)
    (output / "comparison.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "comparison.md").write_text(markdown(result), encoding="utf-8")
    print(f"{result['status']}: {output}")
    return 2 if result["status"] == "VALIDATION_FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
