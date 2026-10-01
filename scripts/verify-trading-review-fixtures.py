"""Static validation for the integration-owned Trading Review golden dataset.

This verifier checks authored fixture structure, provenance, conservation, privacy,
and deterministic serialization. It does not reconstruct position episodes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema


POLICY = "BINANCE_USDS_LINEAR_PERPETUAL_ONE_WAY_V1"
TRADE_HEADERS = ["Uid", "Time", "Symbol", "Side", "Price", "Quantity", "Amount", "Fee", "Realized Profit", "Buyer", "Maker", "Trade ID", "Order ID"]
POSITION_HEADERS = ["Symbol", "Margin Mode", "Position Side", "Entry Price", "Avg. Close Price", "Max Open Interest", "Closed Vol.", "Closing PNL", "Opened", "Closed", "Status"]
EXPECTED_FIXTURES = {
    "simple-long", "simple-short", "partial-entry", "partial-exit", "increase-and-reduce",
    "long-to-short-reversal", "same-timestamp-multiple-trades", "fee-rebate",
    "left-censored", "right-censored", "incomplete-trades", "ambiguous-position-match",
    "position-pnl-mismatch", "different-fee-asset", "unsupported-position-status",
    "duplicate-execution-same-content", "execution-identity-conflict", "hedge-overlap",
    "unsupported-coin-m", "required-column-missing", "unsupported-source-schema",
}
REQUIRED_FILES = {
    "trade-history.csv", "position-history.csv", "fixture-manifest.json",
    "expected/canonical-records.json", "expected/reconciliation-manifest.json",
    "expected/quality-report.json",
}
ELIGIBLE_RECONCILIATION = {"EXACT", "WITHIN_ROUNDING_TOLERANCE"}
KNOWN_SAMPLE_VALUES = {"900000001", "700000001", "800000001", "BTCUSDT", "ETHUSDT"}


class ValidationFailure(Exception):
    pass


def fail(message: str) -> None:
    raise ValidationFailure(message)


def load_json(path: Path, *, canonical_format: bool = True) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"Invalid JSON {path}: {error}")
    if canonical_format:
        expected = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if text != expected:
            fail(f"JSON is not deterministic (UTF-8, sorted keys, LF, final newline): {path}")
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def decimal_value(value: dict[str, Any]) -> Decimal:
    return Decimal(int(value["unscaled"])).scaleb(-int(value["scale"]))


def csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    payload = path.read_bytes()
    if b"\r" in payload or not payload.endswith(b"\n"):
        fail(f"CSV must use LF and a final newline: {path}")
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames or []
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as error:
        fail(f"Invalid CSV {path}: {error}")
    return headers, rows


def validate_schema(instance: Any, schema: Any, path: Path) -> None:
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        fail(f"Schema validation failed {path} at {location}: {error.message}")


def validate_context(context_path: Path) -> None:
    required = {
        "docs/DECISIONS.md": ["ADR-055", "ADR-056", "ADR-057", "ADR-058", "ADR-059"],
        "docs/DOMAIN.md": [*(f"TR-I{number:02d}" for number in range(1, 14))],
        "docs/TRADING_RECONSTRUCTION.md": [POLICY, "RECORD_CONFLICT", "UNSUPPORTED_POSITION_MODE", "UNSUPPORTED_POSITION_STATUS"],
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
    readme = root / "README.md"
    text = readme.read_text(encoding="utf-8")
    for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
        if "://" in target or target.startswith("#"):
            continue
        resolved = (readme.parent / target.split("#", 1)[0]).resolve()
        if not resolved.exists():
            fail(f"Broken relative Markdown link in {readme}: {target}")


def validate_provenance(refs: list[dict[str, Any]], row_counts: dict[str, int], label: str) -> None:
    if not refs:
        fail(f"Missing source provenance: {label}")
    for ref in refs:
        role = ref["artifactRole"]
        row = ref["sourceRowNumber"]
        if role not in row_counts or row < 1 or row > row_counts[role]:
            fail(f"Out-of-range source provenance {role} row {row}: {label}")
        if not ref.get("sourceRecordKey"):
            fail(f"Missing synthetic source record key: {label}")


def validate_namespace(fixture_id: str, trade_rows: list[dict[str, str]], position_rows: list[dict[str, str]], failure_code: str | None) -> None:
    for row_number, row in enumerate(trade_rows, 1):
        if not re.fullmatch(r"99000000[0-9]{4}", row.get("Uid", "")):
            fail(f"Synthetic UID namespace violation in {fixture_id} trade row {row_number}")
        trade_id = row.get("Trade ID", row.get("Trade Id", ""))
        if not re.fullmatch(r"91000000[0-9]{4}", trade_id):
            fail(f"Synthetic Trade ID namespace violation in {fixture_id} trade row {row_number}")
        if not re.fullmatch(r"92000000[0-9]{4}", row.get("Order ID", "")):
            fail(f"Synthetic Order ID namespace violation in {fixture_id} trade row {row_number}")
        symbol_pattern = r"[A-Z]{3}USD" if failure_code == "UNSUPPORTED_PRODUCT" else r"[A-Z]{3}USDT"
        if not re.fullmatch(symbol_pattern, row.get("Symbol", "")):
            fail(f"Synthetic symbol namespace violation in {fixture_id} trade row {row_number}")
    for row_number, row in enumerate(position_rows, 1):
        symbol_pattern = r"[A-Z]{3}USD" if failure_code == "UNSUPPORTED_PRODUCT" else r"[A-Z]{3}USDT"
        if not re.fullmatch(symbol_pattern, row.get("Symbol", "")):
            fail(f"Synthetic symbol namespace violation in {fixture_id} position row {row_number}")


def validate_conservation(canonical: dict[str, Any], reconciliation: dict[str, Any]) -> None:
    records = {record["canonicalRecordId"]: record for record in canonical["records"]}
    quantity = defaultdict(Decimal)
    fee = defaultdict(lambda: defaultdict(Decimal))
    pnl = defaultdict(Decimal)
    for allocation in reconciliation["allocations"]:
        execution_id = allocation["executionRecordId"]
        if execution_id not in records or allocation["feeRecordId"] not in records or allocation["reportedPnlRecordId"] not in records:
            fail(f"Allocation links an unknown canonical record: {execution_id}")
        quantity[execution_id] += decimal_value(allocation["allocatedQuantity"])
        for asset, value in allocation["allocatedFeeByAsset"].items():
            fee[allocation["feeRecordId"]][asset] += decimal_value(value)
        pnl[allocation["reportedPnlRecordId"]] += decimal_value(allocation["allocatedReportedPnl"])
    for record in records.values():
        record_id = record["canonicalRecordId"]
        if record["recordType"] == "EXECUTION" and quantity[record_id] != decimal_value(record["quantity"]):
            fail(f"Execution quantity is not conserved: {record_id}")
        if record["recordType"] == "FEE":
            asset = record["fee"]["asset"]
            if fee[record_id][asset] != decimal_value(record["fee"]["sourceAmount"]):
                fail(f"Fee is not conserved: {record_id}")
        if record["recordType"] == "VENUE_REPORTED_PNL" and pnl[record_id] != decimal_value(record["reportedRealizedPnl"]["amount"]):
            fail(f"Reported PnL is not conserved: {record_id}")
    allocations_by_episode = defaultdict(list)
    for allocation in reconciliation["allocations"]:
        allocations_by_episode[allocation["episodeId"]].append(allocation)
    for episode in reconciliation["episodes"]:
        if episode["reconstructionStatus"] != "COMPLETE":
            continue
        opening = sum((decimal_value(item["allocatedQuantity"]) for item in allocations_by_episode[episode["episodeId"]] if item["action"] in {"OPEN", "INCREASE", "REVERSAL_OPEN"}), Decimal(0))
        closing = sum((decimal_value(item["allocatedQuantity"]) for item in allocations_by_episode[episode["episodeId"]] if item["action"] in {"REDUCE", "CLOSE", "REVERSAL_CLOSE"}), Decimal(0))
        if opening != closing:
            fail(f"Complete episode opening/closing volume is not conserved: {episode['episodeId']}")


def validate_fixture(folder: Path, index_entry: dict[str, Any], schemas: dict[str, Any]) -> None:
    fixture_id = index_entry["fixtureId"]
    present = {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}
    missing = REQUIRED_FILES - present
    if missing:
        fail(f"Missing fixture files for {fixture_id}: {sorted(missing)}")
    if folder.name != fixture_id or index_entry["path"] != fixture_id:
        fail(f"Fixture directory/index identity mismatch: {fixture_id}")

    manifest_path = folder / "fixture-manifest.json"
    canonical_path = folder / "expected" / "canonical-records.json"
    reconciliation_path = folder / "expected" / "reconciliation-manifest.json"
    quality_path = folder / "expected" / "quality-report.json"
    manifest = load_json(manifest_path)
    canonical = load_json(canonical_path)
    reconciliation = load_json(reconciliation_path)
    quality = load_json(quality_path)
    validate_schema(manifest, schemas["fixture-manifest"] , manifest_path)
    validate_schema(canonical, schemas["canonical-records"], canonical_path)
    validate_schema(reconciliation, schemas["reconciliation-manifest"], reconciliation_path)
    validate_schema(quality, schemas["quality-report"], quality_path)
    if any(value["fixtureId"] != fixture_id for value in (manifest, canonical, reconciliation, quality)):
        fail(f"Fixture ID mismatch inside authored JSON: {fixture_id}")

    trade_headers, trade_rows = csv_rows(folder / "trade-history.csv")
    position_headers, position_rows = csv_rows(folder / "position-history.csv")
    failure_code = manifest["expectedFailureCode"]
    if len(position_headers) != len(set(position_headers)) or position_headers != POSITION_HEADERS:
        fail(f"Position History headers do not match V1: {fixture_id}")
    if failure_code == "REQUIRED_COLUMN_MISSING":
        if set(TRADE_HEADERS) - set(trade_headers) != {"Maker"}:
            fail(f"Required-column negative fixture does not isolate Maker: {fixture_id}")
    elif failure_code == "UNSUPPORTED_SOURCE_SCHEMA":
        if "Trade Id" not in trade_headers or "Trade ID" in trade_headers:
            fail(f"Unsupported-schema fixture does not isolate Trade ID rename: {fixture_id}")
    elif trade_headers != TRADE_HEADERS or len(trade_headers) != len(set(trade_headers)):
        fail(f"Trade History headers do not match V1: {fixture_id}")
    validate_namespace(fixture_id, trade_rows, position_rows, failure_code)

    row_counts = {"TRADE_HISTORY": len(trade_rows), "POSITION_HISTORY": len(position_rows)}
    if quality["sourceRowCount"] != row_counts:
        fail(f"Quality source row count mismatch: {fixture_id}")
    if manifest["expectedCanonicalRecordCount"] != len(canonical["records"]) or quality["canonicalRecordCount"] != len(canonical["records"]):
        fail(f"Canonical record count mismatch: {fixture_id}")
    if manifest["expectedEpisodeCount"] != len(reconciliation["episodes"]) or quality["episodeCount"] != len(reconciliation["episodes"]):
        fail(f"Episode count mismatch: {fixture_id}")
    eligible = sum(1 for episode in reconciliation["episodes"] if episode["eligible"])
    excluded = len(reconciliation["episodes"]) - eligible
    if (manifest["expectedEligibleEpisodeCount"], manifest["expectedExcludedEpisodeCount"]) != (eligible, excluded):
        fail(f"Manifest eligible/excluded count mismatch: {fixture_id}")
    if (quality["eligibleEpisodeCount"], quality["excludedEpisodeCount"]) != (eligible, excluded):
        fail(f"Quality eligible/excluded count mismatch: {fixture_id}")
    statuses = sorted({episode["reconciliationStatus"] for episode in reconciliation["episodes"]})
    if manifest["expectedReconciliationStatuses"] != statuses:
        fail(f"Expected reconciliation status mismatch: {fixture_id}")

    record_ids: set[str] = set()
    for record in canonical["records"]:
        record_id = record["canonicalRecordId"]
        if record_id in record_ids:
            fail(f"Duplicate canonical record ID: {record_id}")
        record_ids.add(record_id)
        validate_provenance(record["sourceProvenance"], row_counts, record_id)
        if record["sourceArtifactRole"] not in {ref["artifactRole"] for ref in record["sourceProvenance"]}:
            fail(f"Canonical primary artifact role is absent from provenance: {record_id}")
    episode_ids: set[str] = set()
    canonical_types = {record["canonicalRecordId"]: record["recordType"] for record in canonical["records"]}
    for episode in reconciliation["episodes"]:
        episode_id = episode["episodeId"]
        if episode_id in episode_ids:
            fail(f"Duplicate episode ID: {episode_id}")
        episode_ids.add(episode_id)
        validate_provenance(episode["sourceProvenance"], row_counts, episode_id)
        if any(canonical_types.get(record_id) != "EXECUTION" for record_id in episode["executionRecordIds"]):
            fail(f"Episode links an unknown/non-execution canonical record: {episode_id}")
        if any(canonical_types.get(record_id) != "VENUE_POSITION_SUMMARY" for record_id in episode["venuePositionSummaryRecordIds"]):
            fail(f"Episode links an unknown/non-summary canonical record: {episode_id}")
        if episode["eligible"]:
            if episode["reconstructionStatus"] != "COMPLETE" or episode["reconciliationStatus"] not in ELIGIBLE_RECONCILIATION or "CLOSED_OUTCOME" not in episode["capabilities"]:
                fail(f"Invalid eligible episode state/capability: {episode_id}")
            if episode["exclusionReasons"]:
                fail(f"Eligible episode has exclusion reasons: {episode_id}")
            if not episode["executionRecordIds"] or len(episode["venuePositionSummaryRecordIds"]) != 1:
                fail(f"Eligible episode lacks direct execution/summary evidence: {episode_id}")
        elif not episode["exclusionReasons"]:
            fail(f"Excluded episode has no exclusion reason: {episode_id}")
    for allocation in reconciliation["allocations"]:
        if allocation["episodeId"] not in episode_ids:
            fail(f"Allocation references unknown episode: {allocation['episodeId']}")
        validate_provenance(allocation["sourceProvenance"], row_counts, allocation["executionRecordId"])
    for summary_match in reconciliation["summaryMatches"]:
        validate_provenance(summary_match["sourceProvenance"], row_counts, summary_match["summaryRecordId"])

    expected_accepted = manifest["expectedImportStatus"] == "ACCEPTED"
    if reconciliation["importResult"]["accepted"] != expected_accepted or reconciliation["importResult"]["failureCode"] != failure_code:
        fail(f"Import expectation mismatch: {fixture_id}")
    if expected_accepted and failure_code is not None:
        fail(f"Accepted fixture has a failure code: {fixture_id}")
    if not expected_accepted and not failure_code:
        fail(f"Rejected fixture has no failure code: {fixture_id}")
    if quality["importStatus"] != manifest["expectedImportStatus"]:
        fail(f"Quality import status mismatch: {fixture_id}")

    artifact_roles = [artifact["role"] for artifact in manifest["artifacts"]]
    if sorted(artifact_roles) != ["POSITION_HISTORY", "TRADE_HISTORY"] or len(set(artifact_roles)) != 2:
        fail(f"Artifact roles must appear exactly once: {fixture_id}")
    for artifact in manifest["artifacts"]:
        path = folder / artifact["path"]
        if digest_file(path) != artifact["sha256"] or path.stat().st_size != artifact["sizeBytes"]:
            fail(f"Fixture artifact checksum/size mismatch: {fixture_id}/{artifact['path']}")
    inputs = reconciliation["canonicalManifestHashInputs"]
    if inputs["canonicalRecordContentHash"] != digest_bytes(canonical_bytes(canonical)):
        fail(f"Canonical record content hash mismatch: {fixture_id}")
    for role, filename in {"TRADE_HISTORY": "trade-history.csv", "POSITION_HISTORY": "position-history.csv"}.items():
        if inputs["sourceArtifactHashes"][role] != digest_file(folder / filename):
            fail(f"Manifest source artifact hash mismatch: {fixture_id}/{role}")
    hash_payload = dict(reconciliation)
    recorded_hash = hash_payload.pop("canonicalManifestHash")
    if recorded_hash != digest_bytes(canonical_bytes(hash_payload)):
        fail(f"Canonical reconciliation manifest hash mismatch: {fixture_id}")
    if expected_accepted:
        validate_conservation(canonical, reconciliation)

    indexed_files = {item["path"]: item for item in index_entry["files"]}
    if set(indexed_files) != REQUIRED_FILES:
        fail(f"Fixture index file catalog mismatch: {fixture_id}")
    for relative, metadata in indexed_files.items():
        path = folder / relative
        if digest_file(path) != metadata["sha256"] or path.stat().st_size != metadata["sizeBytes"]:
            fail(f"Fixture index checksum/size mismatch: {fixture_id}/{relative}")


def validate(root: Path, context_path: Path) -> None:
    if not root.is_dir():
        fail(f"Fixture root does not exist: {root}")
    validate_context(context_path)
    validate_markdown_links(root)
    schemas = {
        name: load_json(root / "schemas" / f"{name}.schema.json", canonical_format=False)
        for name in ["fixture-manifest", "canonical-records", "reconciliation-manifest", "quality-report"]
    }
    for name, schema in schemas.items():
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as error:
            fail(f"Invalid JSON Schema {name}: {error.message}")
    index = load_json(root / "fixture-index.json")
    if index["contextContract"]["reconstructionPolicyVersion"] != POLICY:
        fail("Fixture index is not linked to the context reconstruction policy version")
    entries = index["fixtures"]
    fixture_ids = [entry["fixtureId"] for entry in entries]
    if len(fixture_ids) != len(set(fixture_ids)):
        fail("Duplicate fixture ID in fixture-index.json")
    if set(fixture_ids) != EXPECTED_FIXTURES:
        fail(f"Fixture catalog mismatch; missing={sorted(EXPECTED_FIXTURES - set(fixture_ids))}, extra={sorted(set(fixture_ids) - EXPECTED_FIXTURES)}")
    categories = {entry["category"] for entry in entries}
    if categories != {"ACCEPTED_ELIGIBLE", "ACCEPTED_EXCLUDED", "REJECTED_IMPORT"}:
        fail(f"Fixture categories are incomplete: {sorted(categories)}")
    for entry in entries:
        validate_fixture(root / entry["path"], entry, schemas)
    expected_package_files = {"README.md", *{f"schemas/{name}.schema.json" for name in schemas}}
    indexed_package_files = {item["path"]: item for item in index.get("packageFiles", [])}
    if set(indexed_package_files) != expected_package_files:
        fail("Fixture package checksum catalog is incomplete")
    for relative, metadata in indexed_package_files.items():
        path = root / relative
        if digest_file(path) != metadata["sha256"] or path.stat().st_size != metadata["sizeBytes"]:
            fail(f"Fixture package checksum/size mismatch: {relative}")
    searchable = b"\n".join(path.read_bytes() for path in root.rglob("*") if path.is_file() and path.suffix in {".csv", ".json"})
    for forbidden in KNOWN_SAMPLE_VALUES:
        if forbidden.encode() in searchable:
            fail(f"Known real/public sample value found in fixtures: {forbidden}")


def tamper_self_test(root: Path, context_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="refinvest-fixture-tamper-") as temp:
        copied = Path(temp) / root.name
        shutil.copytree(root, copied)
        target = copied / "simple-long" / "expected" / "quality-report.json"
        value = json.loads(target.read_text(encoding="utf-8"))
        value["canonicalRecordCount"] += 1
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        try:
            validate(copied, context_path)
        except ValidationFailure:
            return
        fail("Tamper self-test failed: a corrupted fixture was accepted")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--context-path", type=Path, required=True)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        validate(args.root.resolve(), args.context_path.resolve())
        if args.self_test:
            tamper_self_test(args.root.resolve(), args.context_path.resolve())
    except ValidationFailure as error:
        print(f"FAIL {error}")
        return 1
    print(f"OK  {len(EXPECTED_FIXTURES)} Trading Review fixtures, schemas, hashes, provenance, privacy, and tamper detection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
