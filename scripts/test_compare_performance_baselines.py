"""Synthetic official reports exercise validation and comparison decisions."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from performance_baseline import STAGES, read_object, scenario_definitions, statistics

spec = importlib.util.spec_from_file_location("comparison", ROOT / "scripts/compare-performance-baselines.py")
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def official_report() -> dict:
    protocol = read_object(ROOT / "performance/baseline-protocol.json")
    expected = read_object(ROOT / "performance/expected-results.json")
    expected_map = {item["scenarioId"]: item for item in expected["scenarios"]}
    fixture = expected["fixture"]
    report = {
        "baselineVersion": 1, "baselineId": "synthetic-baseline", "generatedAt": "2026-09-22T00:00:00Z",
        "official": True, "passed": True, "protocol": {
            "protocolVersion": protocol["protocolVersion"], "canonicalizationVersion": protocol["canonicalizationVersion"],
            "cacheMode": protocol["cacheModes"][0], "warmupsPerBatch": protocol["warmupsPerBatch"],
            "iterationsPerBatch": protocol["iterationsPerBatch"], "batches": protocol["batches"],
            "p95Method": protocol["p95Method"], "failurePolicy": protocol["failurePolicy"],
            "environmentPolicy": protocol["environmentPolicy"], "executionOrder": protocol["executionOrder"]},
        "fixture": fixture, "revisions": {"integration": "a" * 40, "compute": "b" * 40},
        "environment": {"environmentId": "test-machine", "storageProfile": "local-ssd", "resourceProfile": "unrestricted", "os": "Windows 11",
                        "platform": "Windows", "machine": "X64", "cpu": "test-cpu", "logicalCpuCount": 8,
                        "memoryBytes": 16_000_000_000, "pythonVersion": "3.11.5", "engineVersion": "0.1.0",
                        "computePackageVersion": "0.1.0", "dependencyLockSha256": "sha256:" + "c" * 64},
        "correctness": {"passed": True, "failures": []}, "scenarios": []}
    definitions = scenario_definitions(ROOT / "performance/scenarios")
    for scenario_id, definition in sorted(definitions.items()):
        expected_item = expected_map[scenario_id]
        samples = []
        batches = []
        for batch in (1, 2):
            batch_samples = []
            for iteration in range(1, 11):
                sample = {"batch": batch, "iteration": iteration, "status": "SUCCEEDED",
                          "totalNanoseconds": 100 + iteration, "stageNanoseconds": {stage: 10 + iteration for stage in STAGES},
                          "pythonPeakAllocatedBytes": 1_000 + iteration,
                          **{field: expected_item[field] for field in ("terminalStatus", "tradeCount", "sampleSizeWarning", "resultHash")}}
                samples.append(sample)
                batch_samples.append(sample)
            batches.append({"batch": batch, "total": statistics([sample["totalNanoseconds"] for sample in batch_samples])})
        peaks = sorted(sample["pythonPeakAllocatedBytes"] for sample in samples)
        report["scenarios"].append({"id": scenario_id, "version": definition["scenarioVersion"],
            "batchCount": 2, "sampleCount": 20, "successfulIterations": 20, "failedIterations": 0,
            "batches": batches, "rawSamples": samples,
            "correctness": {"passed": True, **{field: expected_item[field] for field in ("terminalStatus", "tradeCount", "sampleSizeWarning", "resultHash")}},
            "summary": {"total": statistics([sample["totalNanoseconds"] for sample in samples]),
                        "stages": {stage: statistics([sample["stageNanoseconds"][stage] for sample in samples]) for stage in STAGES},
                        "pythonPeakAllocatedBytes": {"median": (peaks[9] + peaks[10]) // 2, "max": peaks[-1]},
                        "batchMedianSpreadNanoseconds": 0}})
    return report


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.base = official_report()
        self.candidate = copy.deepcopy(self.base)
        self.candidate["baselineId"] = "synthetic-candidate"
        self.candidate["revisions"]["compute"] = "d" * 40
        self.protocol = read_object(ROOT / "performance/baseline-protocol.json")
        self.expected = read_object(ROOT / "performance/expected-results.json")
        self.manifest = read_object(ROOT / "performance/fixture-manifest.json")
        self.definitions = scenario_definitions(ROOT / "performance/scenarios")

    def validate(self, report):
        comparison.validate_report(report, self.protocol, self.expected, self.manifest, self.definitions)

    def test_normal_and_improvement(self):
        self.validate(self.base)
        self.validate(self.candidate)
        reasons, scenarios, warnings = comparison.compare(self.base, self.candidate)
        self.assertEqual((reasons, warnings), ([], []))
        self.assertEqual(len(scenarios), 6)
        for scenario in self.candidate["scenarios"]:
            for sample in scenario["rawSamples"]:
                sample["totalNanoseconds"] //= 2
            for batch in scenario["batches"]:
                values = [sample["totalNanoseconds"] for sample in scenario["rawSamples"] if sample["batch"] == batch["batch"]]
                batch["total"] = statistics(values)
            scenario["summary"]["total"] = statistics([sample["totalNanoseconds"] for sample in scenario["rawSamples"]])
        self.validate(self.candidate)
        _, scenarios, warnings = comparison.compare(self.base, self.candidate)
        self.assertLess(scenarios[0]["metrics"]["totalMedian"]["percentChange"], 0)
        self.assertEqual(warnings, [])

    def test_slowdown_warns_without_failure(self):
        for scenario in self.candidate["scenarios"]:
            for sample in scenario["rawSamples"]:
                sample["totalNanoseconds"] *= 2
            for batch in scenario["batches"]:
                batch["total"] = statistics([sample["totalNanoseconds"] for sample in scenario["rawSamples"] if sample["batch"] == batch["batch"]])
            scenario["summary"]["total"] = statistics([sample["totalNanoseconds"] for sample in scenario["rawSamples"]])
        self.validate(self.candidate)
        reasons, _, warnings = comparison.compare(self.base, self.candidate)
        self.assertEqual(reasons, [])
        self.assertEqual(len(warnings), 12)

    def test_correctness_failed_and_missing_samples(self):
        for mutation in (lambda r: r["scenarios"][0]["rawSamples"][0].update(resultHash="sha256:" + "0" * 64),
                         lambda r: r["scenarios"][0]["rawSamples"].pop(),
                         lambda r: r["scenarios"][0]["rawSamples"][0].update(status="TIMEOUT"),
                         lambda r: r["scenarios"][0]["correctness"].update(tradeCount=-1)):
            report = copy.deepcopy(self.base)
            mutation(report)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.validate(report)

    def test_fixture_protocol_environment_and_zero_denominator(self):
        self.candidate["fixture"]["id"] = "other"
        reasons, _, _ = comparison.compare(self.base, self.candidate)
        self.assertIn("Fixture identity differs.", reasons)
        self.candidate = copy.deepcopy(self.base)
        self.candidate["protocol"]["cacheMode"] = "process-cold"
        reasons, _, _ = comparison.compare(self.base, self.candidate)
        self.assertIn("Protocol differs: cacheMode.", reasons)
        self.candidate = copy.deepcopy(self.base)
        self.candidate["environment"]["cpu"] = "different-cpu"
        reasons, _, _ = comparison.compare(self.base, self.candidate)
        self.assertIn("Environment differs: cpu.", reasons)
        self.assertIsNone(comparison._difference(0, 5, "ns")["percentChange"])

    def test_cli_artifacts_and_exit_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            left, right = directory / "left.json", directory / "right.json"
            left.write_text(json.dumps(self.base), encoding="utf-8")
            right.write_text(json.dumps(self.candidate), encoding="utf-8")
            result = comparison.make_result(left, right, ROOT / "performance/baseline-protocol.json",
                ROOT / "performance/expected-results.json", ROOT / "performance/fixture-manifest.json", ROOT / "performance/scenarios")
            self.assertEqual(result["status"], "COMPARABLE")
            self.assertIn("totalMedian", comparison.markdown(result))
            right.unlink()
            result = comparison.make_result(left, right, ROOT / "performance/baseline-protocol.json",
                ROOT / "performance/expected-results.json", ROOT / "performance/fixture-manifest.json", ROOT / "performance/scenarios")
            self.assertEqual(result["status"], "VALIDATION_FAILED")
            self.assertEqual(result["validation"]["candidate"], "FAIL")

    def test_cli_exit_codes_and_saved_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            left, right, outputs = directory / "left.json", directory / "right.json", directory / "results"
            left.write_text(json.dumps(self.base), encoding="utf-8")
            right.write_text(json.dumps(self.candidate), encoding="utf-8")
            command = [sys.executable, "-B", str(ROOT / "scripts/compare-performance-baselines.py"),
                       "--baseline", str(left), "--candidate", str(right), "--output-root", str(outputs)]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = list(outputs.glob("*/comparison.json"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(read_object(saved[0])["status"], "COMPARABLE")
            self.assertTrue(saved[0].with_suffix(".md").exists())
            incorrect = copy.deepcopy(self.candidate)
            incorrect["scenarios"][0]["rawSamples"][0]["resultHash"] = "sha256:" + "0" * 64
            right.write_text(json.dumps(incorrect), encoding="utf-8")
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(read_object(sorted(outputs.glob("*/comparison.json"))[-1])["status"], "VALIDATION_FAILED")
            right.unlink()
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(len(list(outputs.glob("*/comparison.json"))), 3)


if __name__ == "__main__":
    unittest.main()
