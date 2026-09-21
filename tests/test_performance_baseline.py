from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from performance_baseline import STAGES, BaselineError, statistics


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_script("baseline_builder", "build-performance-baseline-report.py")
verifier = load_script("baseline_verifier", "verify-performance-baseline.py")


class PerformanceBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = {
            "protocolVersion": 1, "canonicalizationVersion": 1, "cacheModes": ["warm"],
            "warmupsPerBatch": 1, "iterationsPerBatch": 2, "batches": 2,
            "p95Method": "nearest-rank", "failurePolicy": "fail-entire-scenario",
            "environmentPolicy": "same-machine-and-runtime", "executionOrder": "test-order",
        }
        self.fixture = {
            "fixtureId": "fixture-v1", "fixtureDefinitionVersion": 1,
            "storageIndependentManifestSha256": "a" * 64,
        }
        self.definitions = {
            "alpha": {"scenarioId": "alpha", "scenarioVersion": 1, "canonicalizationVersion": 1}
        }
        self.expected = {
            "fixture": {"id": "fixture-v1", "version": 1, "manifestSha256": "sha256:" + "a" * 64},
            "scenarios": [{
                "fixtureId": "fixture-v1", "scenarioId": "alpha", "scenarioVersion": 1,
                "canonicalizationVersion": 1, "terminalStatus": "COMPLETED", "tradeCount": 3,
                "sampleSizeWarning": None, "resultHash": "sha256:" + "b" * 64,
            }],
        }
        self.environment = {"os": "test-os", "pythonVersion": "3.11", "engineVersion": "0.1.0"}
        self.revisions = {"integration": "1" * 40, "compute": "2" * 40, "context": None, "core": None, "web": None}

    def report(self, scenario: str = "alpha", fixture_id: str = "fixture-v1", canonical: int = 1):
        samples = []
        for iteration, total in enumerate((10, 20), 1):
            samples.append({
                "iteration": iteration, "status": "SUCCEEDED", "totalNanoseconds": total,
                "stageNanoseconds": {stage: iteration for stage in STAGES},
                "pythonPeakAllocatedBytes": 100 + iteration, "resultHash": "sha256:" + "b" * 64,
                "terminalStatus": "COMPLETED", "tradeCount": 3, "sampleSizeWarning": None,
            })
        return {
            "reportVersion": 1, "canonicalizationVersion": canonical,
            "fixture": {"id": fixture_id, "version": 1, "manifestSha256": "sha256:" + "a" * 64},
            "scenario": {"id": scenario, "version": 1},
            "execution": {"cacheMode": "warm", "warmups": 1, "iterations": 2},
            "samples": samples,
            "correctness": {"terminalStatus": "COMPLETED", "tradeCount": 3,
                            "sampleSizeWarning": None, "resultHash": "sha256:" + "b" * 64,
                            "passed": True, "mismatches": []},
        }

    def build(self):
        temporary = tempfile.TemporaryDirectory()
        directory = Path(temporary.name)
        for batch in (1, 2):
            (directory / f"alpha-batch-{batch}.json").write_text(json.dumps(self.report()), encoding="utf-8")
        baseline = builder.build_baseline(
            protocol=self.protocol, expected=self.expected, manifest=self.fixture,
            scenarios=self.definitions, reports_directory=directory,
            environment=self.environment, revisions=self.revisions, official=False,
            effective={"cacheMode": "warm", "warmupsPerBatch": 1, "iterationsPerBatch": 2, "batches": 2},
            baseline_id="test", generated_at="2026-01-01T00:00:00Z",
        )
        return temporary, baseline

    def test_statistics_use_integer_median_and_nearest_rank_p95(self):
        self.assertEqual(statistics([1, 2, 3, 100]), {
            "minNanoseconds": 1, "maxNanoseconds": 100,
            "medianNanoseconds": 2, "p95Nanoseconds": 100,
        })

    def test_aggregation_merges_batches_and_calculates_spread(self):
        temporary, baseline = self.build()
        self.addCleanup(temporary.cleanup)
        scenario = baseline["scenarios"][0]
        self.assertEqual(scenario["sampleCount"], 4)
        self.assertEqual(scenario["summary"]["total"]["medianNanoseconds"], 15)
        self.assertEqual(scenario["summary"]["batchMedianSpreadNanoseconds"], 0)
        verifier.verify(baseline, self.protocol, self.expected, self.fixture, self.definitions)

    def test_failed_sample_is_preserved_and_marks_baseline_failed(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        for batch in (1, 2):
            report = self.report()
            if batch == 2:
                report["samples"][1].update(status="FAILED", resultHash=None, terminalStatus="FAILED", tradeCount=None)
                report["correctness"]["passed"] = False
            (directory / f"alpha-batch-{batch}.json").write_text(json.dumps(report), encoding="utf-8")
        baseline = builder.build_baseline(
            protocol=self.protocol, expected=self.expected, manifest=self.fixture,
            scenarios=self.definitions, reports_directory=directory, environment=self.environment,
            revisions=self.revisions, official=False,
            effective={"cacheMode": "warm", "warmupsPerBatch": 1, "iterationsPerBatch": 2, "batches": 2},
            baseline_id="test", generated_at="2026-01-01T00:00:00Z")
        self.assertFalse(baseline["passed"])
        self.assertEqual(baseline["scenarios"][0]["failedIterations"], 1)
        self.assertEqual(len(baseline["scenarios"][0]["rawSamples"]), 4)

    def test_builder_rejects_identity_and_canonicalization_mismatches(self):
        for report in (self.report(scenario="other"), self.report(fixture_id="other"), self.report(canonical=2)):
            with (
                self.subTest(report=report["scenario"], canonical=report["canonicalizationVersion"]),
                tempfile.TemporaryDirectory() as name,
            ):
                    directory = Path(name)
                    for batch in (1, 2):
                        (directory / f"alpha-batch-{batch}.json").write_text(json.dumps(report), encoding="utf-8")
                    with self.assertRaises(BaselineError):
                        builder.build_baseline(
                            protocol=self.protocol, expected=self.expected, manifest=self.fixture,
                            scenarios=self.definitions, reports_directory=directory,
                            environment=self.environment, revisions=self.revisions, official=False,
                            effective={"cacheMode": "warm", "warmupsPerBatch": 1, "iterationsPerBatch": 2, "batches": 2},
                            baseline_id="test", generated_at="2026-01-01T00:00:00Z")

    def test_verifier_rejects_structural_correctness_summary_and_leak_mutations(self):
        temporary, baseline = self.build()
        self.addCleanup(temporary.cleanup)
        mutations = []
        missing_scenario = copy.deepcopy(baseline); missing_scenario["scenarios"] = []
        mutations.append(missing_scenario)
        missing_batch = copy.deepcopy(baseline); missing_batch["scenarios"][0]["batches"].pop()
        mutations.append(missing_batch)
        missing_iteration = copy.deepcopy(baseline); missing_iteration["scenarios"][0]["rawSamples"].pop()
        mutations.append(missing_iteration)
        for field, value in (("resultHash", "sha256:" + "c" * 64), ("terminalStatus", "FAILED"),
                             ("tradeCount", 4), ("sampleSizeWarning", "ZERO")):
            changed = copy.deepcopy(baseline); changed["scenarios"][0]["correctness"][field] = value
            mutations.append(changed)
        negative = copy.deepcopy(baseline); negative["scenarios"][0]["rawSamples"][0]["totalNanoseconds"] = -1
        mutations.append(negative)
        missing_stage = copy.deepcopy(baseline); missing_stage["scenarios"][0]["rawSamples"][0]["stageNanoseconds"].pop(STAGES[0])
        mutations.append(missing_stage)
        bad_summary = copy.deepcopy(baseline); bad_summary["scenarios"][0]["summary"]["total"]["medianNanoseconds"] += 1
        mutations.append(bad_summary)
        path_leak = copy.deepcopy(baseline); path_leak["environment"]["note"] = "C:\\Users\\person\\secret.txt"
        mutations.append(path_leak)
        sensitive = copy.deepcopy(baseline); sensitive["environment"]["apiKey"] = "value"
        mutations.append(sensitive)
        for index, changed in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(BaselineError):
                verifier.verify(changed, self.protocol, self.expected, self.fixture, self.definitions)


if __name__ == "__main__":
    unittest.main()
