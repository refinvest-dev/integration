"""Validate authored Trading Review analytics golden fixtures without calculating metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import jsonschema


POLICY = "BINANCE_USDS_LINEAR_PERPETUAL_ONE_WAY_V1"
BUNDLE = "TRADING_BEHAVIOR_METRICS_V1"
DEFINITIONS = {
    "ENTRY_WITHIN_WINDOW_AFTER_LOSS": 1,
    "INITIAL_EXPOSURE_CHANGE_AFTER_LOSS": 1,
    "WIN_LOSS_HOLDING_DURATION_DIFFERENCE": 1,
}
STATUSES = {"AVAILABLE", "INSUFFICIENT_SAMPLE", "MISSING_CAPABILITY", "NOT_APPLICABLE"}
REQUIRED_FILES = {
    "fixture-manifest.json",
    "input/episodes.json",
    "input/reconciliation-manifest.json",
    "input/analysis-config.json",
    "expected/observations.json",
    "expected/metrics.json",
    "expected/evidence.json",
    "expected/exclusions.json",
}
SCHEMAS = {
    "fixture-manifest": "analytics-fixture-manifest.schema.json",
    "episodes": "episodes.schema.json",
    "analysis-config": "analysis-config.schema.json",
    "observations": "observations.schema.json",
    "metrics": "metrics.schema.json",
    "evidence": "evidence.schema.json",
    "exclusions": "exclusions.schema.json",
}
FIXTURE_FILES = {
    "fixture-manifest.json": "fixture-manifest",
    "input/episodes.json": "episodes",
    "input/analysis-config.json": "analysis-config",
    "expected/observations.json": "observations",
    "expected/metrics.json": "metrics",
    "expected/evidence.json": "evidence",
    "expected/exclusions.json": "exclusions",
}
EXPECTED_FIXTURES = {
    "reentry-before-boundary",
    "reentry-exact-boundary",
    "reentry-after-boundary",
    "reentry-right-observation-censored",
    "reentry-missing-net-pnl",
    "exposure-increase",
    "exposure-different-settlement",
    "holding-duration-available",
    "holding-duration-win-insufficient",
    "holding-duration-missing-capability",
}
FORBIDDEN_VALUES = {"900000001", "700000001", "800000001", "BTCUSDT", "ETHUSDT"}


class ValidationFailure(Exception):
    pass


def fail(message: str) -> None:
    raise ValidationFailure(message)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def load_json(path: Path, *, canonical: bool = True) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"Invalid JSON {path}: {error}")
    if canonical:
        expected = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if text != expected:
            fail(f"JSON is not deterministic UTF-8/sorted/LF: {path}")
    return value


def validate_context(context_path: Path) -> None:
    required = {
        "docs/DECISIONS.md": [*(f"ADR-0{number}" for number in range(55, 61))],
        "docs/DOMAIN.md": [*(f"TR-I{number:02d}" for number in range(1, 14))],
        "docs/TRADING_ANALYTICS.md": [BUNDLE, *DEFINITIONS, "0 < elapsed <= reentryWindow"],
    }
    for relative, tokens in required.items():
        path = context_path / relative
        if not path.is_file():
            fail(f"Context contract file is missing: {path}")
        text = path.read_text(encoding="utf-8")
        for token in tokens:
            if token not in text:
                fail(f"Context contract token {token!r} is missing from {path}")


def validate_markdown_links(root: Path) -> None:
    path = root / "README.md"
    text = path.read_text(encoding="utf-8")
    for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
        if "://" in target or target.startswith("#"):
            continue
        resolved = (path.parent / target.split("#", 1)[0]).resolve()
        if not resolved.exists():
            fail(f"Broken relative Markdown link: {target}")


def validate_schema(value: Any, schema: Any, path: Path) -> None:
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        fail(f"Schema validation failed {path} at {location}: {error.message}")


def require_unique(values: list[str], label: str) -> set[str]:
    if len(values) != len(set(values)):
        fail(f"Duplicate ID in {label}")
    return set(values)


def without_hash(value: dict[str, Any], field: str) -> dict[str, Any]:
    payload = dict(value)
    payload.pop(field, None)
    return payload


def validate_metric(metric: dict[str, Any], episode_ids: set[str], observation_ids: set[str], evidence_ids: set[str], exclusions: list[dict[str, Any]]) -> None:
    definition_id = metric["definitionId"]
    if DEFINITIONS.get(definition_id) != metric["definitionVersion"]:
        fail(f"Unknown metric definition/version: {definition_id}/{metric['definitionVersion']}")
    if metric["metricStatus"] not in STATUSES:
        fail(f"Unknown MetricStatus: {metric['metricStatus']}")
    for field in ("populationEpisodeIds", "eligibleEpisodeIds", "excludedEpisodeIds", "subjectEpisodeIds", "comparisonEpisodeIds"):
        values = metric[field]
        if len(values) != len(set(values)):
            fail(f"Duplicate episode membership in {metric['behaviorMetricId']}:{field}")
        if not set(values) <= episode_ids:
            fail(f"Unknown episode membership in {metric['behaviorMetricId']}:{field}")
    expected_counts = {
        "populationCount": len(metric["populationEpisodeIds"]),
        "eligibleCount": len(metric["eligibleEpisodeIds"]),
        "excludedCount": len(metric["excludedEpisodeIds"]),
        "subjectCount": len(metric["subjectEpisodeIds"]),
        "comparisonCount": len(metric["comparisonEpisodeIds"]),
    }
    for field, expected in expected_counts.items():
        if metric[field] != expected:
            fail(f"Metric count mismatch {metric['behaviorMetricId']}:{field}")
    if set(metric["eligibleEpisodeIds"]) & set(metric["excludedEpisodeIds"]):
        fail(f"Eligible/excluded overlap: {metric['behaviorMetricId']}")
    if set(metric["eligibleEpisodeIds"]) | set(metric["excludedEpisodeIds"]) != set(metric["populationEpisodeIds"]):
        fail(f"Eligible/excluded membership does not partition population: {metric['behaviorMetricId']}")
    if not set(metric["subjectEpisodeIds"]) <= set(metric["eligibleEpisodeIds"]) or not set(metric["comparisonEpisodeIds"]) <= set(metric["eligibleEpisodeIds"]):
        fail(f"Subject/comparison membership is not eligible: {metric['behaviorMetricId']}")
    reasons = Counter(
        item["exclusionReason"]
        for item in exclusions
        if definition_id in item["metricDefinitionIds"]
    )
    if dict(sorted(reasons.items())) != metric["exclusionReasonCounts"]:
        fail(f"Exclusion reason counts mismatch: {metric['behaviorMetricId']}")
    status = metric["metricStatus"]
    if status == "AVAILABLE":
        if metric["result"] is None or metric["unit"] is None:
            fail(f"AVAILABLE metric lacks value/unit: {metric['behaviorMetricId']}")
        if metric["missingCapabilities"] or metric["sampleCounts"] is not None or metric["applicabilityReason"] is not None:
            fail(f"AVAILABLE metric carries unavailable-state fields: {metric['behaviorMetricId']}")
    elif status == "MISSING_CAPABILITY":
        if metric["result"] is not None or not metric["missingCapabilities"]:
            fail(f"MISSING_CAPABILITY contract violation: {metric['behaviorMetricId']}")
    elif status == "INSUFFICIENT_SAMPLE":
        if metric["result"] is not None or metric["sampleCounts"] is None:
            fail(f"INSUFFICIENT_SAMPLE contract violation: {metric['behaviorMetricId']}")
    elif status == "NOT_APPLICABLE":
        if metric["result"] is not None or not metric["applicabilityReason"]:
            fail(f"NOT_APPLICABLE contract violation: {metric['behaviorMetricId']}")
    if metric["evidenceReference"] not in evidence_ids:
        fail(f"Metric evidence reference is broken: {metric['behaviorMetricId']}")
    if not set(metric["observationIds"]) <= observation_ids:
        fail(f"Metric observation reference is broken: {metric['behaviorMetricId']}")
    if metric["manualVerification"]["method"] != "HAND_CALCULATED_INDEPENDENT_DOUBLE_CHECK":
        fail(f"Metric is not marked as independently hand-checked: {metric['behaviorMetricId']}")
    if metric["manualVerification"]["firstCheck"] == metric["manualVerification"]["secondCheck"]:
        fail(f"Metric manual checks are not independent: {metric['behaviorMetricId']}")


def validate_fixture(folder: Path, index_entry: dict[str, Any], schemas: dict[str, Any]) -> None:
    fixture_id = index_entry["fixtureId"]
    if folder.name != fixture_id or index_entry["path"] != fixture_id:
        fail(f"Fixture directory/index mismatch: {fixture_id}")
    present = {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}
    if present != REQUIRED_FILES:
        fail(f"Fixture file catalog mismatch {fixture_id}: {sorted(present ^ REQUIRED_FILES)}")
    loaded: dict[str, Any] = {}
    for relative, schema_name in FIXTURE_FILES.items():
        path = folder / relative
        loaded[relative] = load_json(path)
        validate_schema(loaded[relative], schemas[schema_name], path)
    reconciliation_path = folder / "input/reconciliation-manifest.json"
    reconciliation = load_json(reconciliation_path)
    manifest = loaded["fixture-manifest.json"]
    episodes = loaded["input/episodes.json"]
    config = loaded["input/analysis-config.json"]
    observations = loaded["expected/observations.json"]
    metrics = loaded["expected/metrics.json"]
    evidence = loaded["expected/evidence.json"]
    exclusions_doc = loaded["expected/exclusions.json"]
    documents = [manifest, episodes, reconciliation, observations, metrics, evidence, exclusions_doc]
    if any(value["fixtureId"] != fixture_id for value in documents):
        fail(f"Embedded fixtureId mismatch: {fixture_id}")
    if config["metricDefinitionVersions"] != {key: DEFINITIONS[key] for key in config["metricDefinitionIds"]}:
        fail(f"AnalysisConfig definition/version mismatch: {fixture_id}")
    if config["metricBundleId"] != BUNDLE or config["reentryWindow"] != "PT30M":
        fail(f"AnalysisConfig contract mismatch: {fixture_id}")
    config_hash = digest_bytes(canonical_bytes(config))
    if metrics["analysisConfigHash"] != config_hash:
        fail(f"AnalysisConfig hash mismatch: {fixture_id}")
    if reconciliation["reconstructionPolicyVersion"] != POLICY:
        fail(f"Reconstruction policy mismatch: {fixture_id}")
    recorded_manifest_hash = reconciliation["manifestHash"]
    if recorded_manifest_hash != digest_bytes(canonical_bytes(without_hash(reconciliation, "manifestHash"))):
        fail(f"Reconciliation manifest hash mismatch: {fixture_id}")
    if manifest["reconciliationManifestHash"] != recorded_manifest_hash:
        fail(f"Fixture/reconciliation hash mismatch: {fixture_id}")
    if manifest["sourceReconstructionFixture"]["sourceManifestHash"] != recorded_manifest_hash:
        fail(f"Source reconstruction manifest hash mismatch: {fixture_id}")
    if manifest["canonicalEpisodeHash"] != digest_bytes(canonical_bytes(episodes)):
        fail(f"Canonical episode hash mismatch: {fixture_id}")
    if set(manifest["metricDefinitionIds"]) != set(config["metricDefinitionIds"]):
        fail(f"Manifest/config metric IDs mismatch: {fixture_id}")
    if manifest["metricDefinitionVersions"] != {key: DEFINITIONS[key] for key in manifest["metricDefinitionIds"]}:
        fail(f"Manifest definition/version mismatch: {fixture_id}")

    episode_ids = require_unique([item["episodeId"] for item in episodes["episodes"]], f"{fixture_id}:episodes")
    if set(reconciliation["episodeSet"]) != episode_ids:
        fail(f"Reconciliation episode set mismatch: {fixture_id}")
    episode_by_id = {item["episodeId"]: item for item in episodes["episodes"]}
    snapshots = {item["episodeId"]: item for item in reconciliation["episodeSnapshots"]}
    if set(snapshots) != episode_ids:
        fail(f"Reconciliation episode snapshot mismatch: {fixture_id}")
    for episode_id, snapshot in snapshots.items():
        item = episode_by_id[episode_id]
        for field in ("openedAt", "closedAt", "reconstructionStatus", "reconciliationStatus", "capabilities"):
            if snapshot[field] != item[field]:
                fail(f"Pinned episode snapshot differs from input episode: {episode_id}/{field}")
    allocation_ids = require_unique([item["allocationId"] for item in reconciliation["positionAllocations"]], f"{fixture_id}:allocations")
    record_ids = require_unique([item["tradingRecordId"] for item in reconciliation["tradingRecords"]], f"{fixture_id}:records")
    source_refs: set[tuple[str, int, str]] = set()
    for artifact in reconciliation["sourceArtifacts"]:
        if not artifact["artifactId"].startswith(f"synthetic:analytics-v1:{fixture_id}:") or artifact["format"] != "CSV":
            fail(f"Synthetic source artifact contract violation: {fixture_id}")
        rows = artifact["sourceRows"]
        if artifact["rowCount"] != len(rows) or artifact["contentHash"] != digest_bytes(canonical_bytes(rows)):
            fail(f"Source artifact row count/hash mismatch: {fixture_id}")
        for row in rows:
            key = (artifact["artifactId"], row["rowNumber"], row["sourceRecordKey"])
            if key in source_refs or not row["sourceRecordKey"].startswith(f"synthetic:analytics-v1:{fixture_id}:"):
                fail(f"Duplicate or non-synthetic source row: {fixture_id}")
            source_refs.add(key)
    for episode in episodes["episodes"]:
        if not episode["episodeId"].startswith(f"synthetic:analytics-v1:{fixture_id}:"):
            fail(f"Synthetic episode namespace violation: {episode['episodeId']}")
        if not re.fullmatch(r"SYNTHETIC:USDS_FUTURES:[A-Z]{3}USDT", episode["instrumentId"]):
            fail(f"Synthetic instrument namespace violation: {episode['instrumentId']}")
        if not set(episode["positionAllocationIds"]) <= allocation_ids:
            fail(f"Episode allocation reference is broken: {episode['episodeId']}")
        for ref in episode["sourceEvidenceRefs"]:
            if (ref["artifactId"], ref["sourceRowNumber"], ref["sourceRecordKey"]) not in source_refs:
                fail(f"Episode source row reference is broken: {episode['episodeId']}")
    for allocation in reconciliation["positionAllocations"]:
        if not allocation["allocationId"].startswith(f"synthetic:analytics-v1:{fixture_id}:"):
            fail(f"Synthetic allocation namespace violation: {allocation['allocationId']}")
        if allocation["episodeId"] not in episode_ids or not set(allocation["tradingRecordIds"]) <= record_ids:
            fail(f"Allocation chain is broken: {allocation['allocationId']}")
        for ref in allocation["sourceEvidenceRefs"]:
            if (ref["artifactId"], ref["sourceRowNumber"], ref["sourceRecordKey"]) not in source_refs:
                fail(f"Allocation source row reference is broken: {allocation['allocationId']}")
    for record in reconciliation["tradingRecords"]:
        if not record["tradingRecordId"].startswith(f"synthetic:analytics-v1:{fixture_id}:"):
            fail(f"Synthetic TradingRecord namespace violation: {record['tradingRecordId']}")
        for ref in record["sourceEvidenceRefs"]:
            if (ref["artifactId"], ref["sourceRowNumber"], ref["sourceRecordKey"]) not in source_refs:
                fail(f"TradingRecord source row reference is broken: {record['tradingRecordId']}")
    observation_ids = require_unique([item["observationId"] for item in observations["observations"]], f"{fixture_id}:observations")
    evidence_ids = require_unique([item["evidenceId"] for item in evidence["evidenceChains"]], f"{fixture_id}:evidence")
    evidence_by_id = {item["evidenceId"]: item for item in evidence["evidenceChains"]}
    metric_ids = require_unique([item["behaviorMetricId"] for item in metrics["metrics"]], f"{fixture_id}:metrics")
    exclusions = exclusions_doc["exclusions"]
    for item in observations["observations"]:
        if item["subjectEpisodeId"] not in episode_ids or not set(item["relatedEpisodeIds"]) <= episode_ids:
            fail(f"Observation episode reference is broken: {item['observationId']}")
        if not set(item["supportingRecordIds"]) <= record_ids:
            fail(f"Observation record reference is broken: {item['observationId']}")
        if item["evidenceReference"] not in evidence_ids:
            fail(f"Observation evidence reference is broken: {item['observationId']}")
    for item in exclusions:
        if item["candidateId"] not in episode_ids or not item["exclusionReason"]:
            fail(f"Exclusion candidate/reason is invalid: {fixture_id}")
        if not item["sourceEvidenceRefs"]:
            fail(f"Exclusion lacks source evidence: {fixture_id}")
        for ref in item["sourceEvidenceRefs"]:
            if (ref["artifactId"], ref["sourceRowNumber"], ref["sourceRecordKey"]) not in source_refs:
                fail(f"Exclusion source row reference is broken: {fixture_id}")
    for chain in evidence["evidenceChains"]:
        if chain["behaviorMetricId"] not in metric_ids:
            fail(f"Evidence metric link is broken: {chain['evidenceId']}")
        if chain["behaviorObservationId"] is not None and chain["behaviorObservationId"] not in observation_ids:
            fail(f"Evidence observation link is broken: {chain['evidenceId']}")
        if chain["episodeId"] not in episode_ids or not set(chain["positionAllocationIds"]) <= allocation_ids or not set(chain["tradingRecordIds"]) <= record_ids:
            fail(f"Evidence episode/allocation/record chain is broken: {chain['evidenceId']}")
        for ref in chain["sourceRowRefs"]:
            if (ref["artifactId"], ref["sourceRowNumber"], ref["sourceRecordKey"]) not in source_refs:
                fail(f"Evidence source row link is broken: {chain['evidenceId']}")
    for metric in metrics["metrics"]:
        validate_metric(metric, episode_ids, observation_ids, evidence_ids, exclusions)
        if evidence_by_id[metric["evidenceReference"]]["behaviorMetricId"] != metric["behaviorMetricId"]:
            fail(f"Metric evidence points to another metric: {metric['behaviorMetricId']}")
    metrics_by_definition = {item["definitionId"]: item for item in metrics["metrics"]}
    for item in exclusions:
        for definition_id in item["metricDefinitionIds"]:
            if definition_id not in metrics_by_definition or item["candidateId"] not in metrics_by_definition[definition_id]["excludedEpisodeIds"]:
                fail(f"Exclusion is not represented in metric membership: {fixture_id}/{definition_id}")
    if {item["definitionId"]: item["metricStatus"] for item in metrics["metrics"]} != manifest["expectedMetricStatuses"]:
        fail(f"Manifest MetricStatus mismatch: {fixture_id}")
    expected_hash = digest_bytes(canonical_bytes({"observations": observations, "metrics": metrics, "evidence": evidence, "exclusions": exclusions_doc}))
    if manifest["expectedResultHash"] != expected_hash or index_entry["expectedResultHash"] != expected_hash:
        fail(f"Expected result hash mismatch: {fixture_id}")
    indexed = {item["path"]: item for item in index_entry["files"]}
    if set(indexed) != REQUIRED_FILES:
        fail(f"Index file catalog mismatch: {fixture_id}")
    for relative, metadata in indexed.items():
        path = folder / relative
        if digest_file(path) != metadata["sha256"] or path.stat().st_size != metadata["sizeBytes"]:
            fail(f"Index checksum mismatch: {fixture_id}/{relative}")


def validate(root: Path, context_path: Path) -> None:
    if not root.is_dir():
        fail(f"Fixture root does not exist: {root}")
    validate_context(context_path)
    validate_markdown_links(root)
    schemas = {name: load_json(root / "schemas" / filename, canonical=False) for name, filename in SCHEMAS.items()}
    for name, schema in schemas.items():
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as error:
            fail(f"Invalid JSON Schema {name}: {error.message}")
    index = load_json(root / "fixture-index.json")
    if index["metricBundleId"] != BUNDLE or index["reconstructionPolicyVersion"] != POLICY:
        fail("Fixture index is not pinned to the context bundle/policy")
    entries = index["fixtures"]
    fixture_ids = [entry["fixtureId"] for entry in entries]
    if len(fixture_ids) != len(set(fixture_ids)) or set(fixture_ids) != EXPECTED_FIXTURES:
        fail(f"Fixture catalog mismatch: {sorted(set(fixture_ids) ^ EXPECTED_FIXTURES)}")
    for entry in entries:
        validate_fixture(root / entry["path"], entry, schemas)
    package_paths = {"README.md", *{f"schemas/{filename}" for filename in SCHEMAS.values()}}
    package_index = {item["path"]: item for item in index["packageFiles"]}
    if set(package_index) != package_paths:
        fail("Package checksum catalog mismatch")
    for relative, metadata in package_index.items():
        path = root / relative
        if digest_file(path) != metadata["sha256"] or path.stat().st_size != metadata["sizeBytes"]:
            fail(f"Package checksum mismatch: {relative}")
    searchable = b"\n".join(path.read_bytes() for path in root.rglob("*") if path.is_file() and path.suffix in {".json", ".md"})
    for forbidden in FORBIDDEN_VALUES:
        if forbidden.encode() in searchable:
            fail(f"Known non-fixture identifier found: {forbidden}")


def tamper_self_test(root: Path, context_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="refinvest-analytics-tamper-") as temp:
        mutations = {
            "broken-count": lambda value: value["metrics"][0].__setitem__("populationCount", value["metrics"][0]["populationCount"] + 1),
            "broken-reference": lambda value: value["metrics"][0]["subjectEpisodeIds"].__setitem__(0, "synthetic:analytics-v1:unknown:episode:x"),
        }
        for name, mutate in mutations.items():
            copied = Path(temp) / name
            shutil.copytree(root, copied)
            target = copied / "reentry-before-boundary" / "expected" / "metrics.json"
            value = json.loads(target.read_text(encoding="utf-8"))
            mutate(value)
            target.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
            try:
                validate(copied, context_path)
            except ValidationFailure:
                continue
            fail(f"Tamper self-test failed: {name} was accepted")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--context-path", required=True, type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        validate(args.root.resolve(), args.context_path.resolve())
        if args.self_test:
            tamper_self_test(args.root.resolve(), args.context_path.resolve())
    except ValidationFailure as error:
        print(f"FAIL {error}")
        return 1
    print(f"OK  {len(EXPECTED_FIXTURES)} Trading Analytics fixtures, schemas, references, hashes, privacy, and tamper detection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
