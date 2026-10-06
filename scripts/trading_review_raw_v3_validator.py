"""Static validator for the synthetic Binance raw-contract fixture set v3.

The module validates authored bytes and expected evidence. It deliberately does
not derive position transitions, episodes, allocations, or reconciliation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import jsonschema


POLICY = "BINANCE_USDS_LINEAR_PERPETUAL_ONE_WAY_V1"
TRADE_DIALECT = "BINANCE_USDS_TRADE_HISTORY_V1"
POSITION_DIALECT = "BINANCE_USDS_POSITION_HISTORY_V1"
TRADE_HEADERS = ["Uid", "Time", "Symbol", "Side", "Price", "Quantity", "Amount", "Fee", "Realized Profit", "Buyer", "Maker", "Trade ID", "Order ID"]
POSITION_HEADERS = ["Symbol", "Margin Mode", "Position Side", "Entry Price", "Avg. Close Price", "Max Open Interest", "Closed Vol.", "Closing PNL", "Opened", "Closed", "Status"]
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
DECIMAL = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SPECIAL_FIXTURES = {
    "raw-byte-dialect",
    "second-precision-position",
    "source-order-scrambled",
    "unicode-symbol",
    "multiple-fills-same-order",
    "left-censored-before-trade-coverage",
    "episode-after-position-coverage",
    "common-window-exact-reconciliation",
    "closing-pnl-excludes-fee",
    "exact-timestamp-non-unique-match",
}
EXPECTED_CONTEXT_SCENARIOS = {
    f"TR-RECON-SCENARIO-{number:02d}": fixture_id
    for number, fixture_id in enumerate(
        [
            "simple-long", "simple-short", "partial-entry", "partial-exit", "increase-and-reduce",
            "loss-context-increase", "long-to-short-reversal", "short-to-long-reversal", "left-censored",
            "right-censored", "same-timestamp-multiple-trades", "invalid-trade-id", "incomplete-trades",
            "episode-after-position-coverage", "position-summary-only", "exact-timestamp-non-unique-match",
            "duplicate-execution-same-content", "execution-identity-conflict", "fee-rebate",
            "different-fee-asset", "hedge-overlap", "unsupported-position-status", "position-pnl-mismatch",
            "within-rounding-tolerance", "above-rounding-tolerance",
        ],
        1,
    )
}
REQUIRED_EXPECTED = {
    "expected/canonical-records.json",
    "expected/reconciliation-manifest.json",
    "expected/quality-report.json",
}


class RawV3ValidationFailure(Exception):
    pass


def fail(message: str) -> None:
    raise RawV3ValidationFailure(message)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def digest_file(path: Path) -> str:
    return digest(path.read_bytes())


def load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"Invalid JSON {path}: {error}")
    expected = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if text != expected:
        fail(f"JSON is not deterministic UTF-8/sorted/LF/final-newline: {path}")
    return value


def decimal_value(value: dict[str, Any]) -> Decimal:
    return Decimal(int(value["unscaled"])).scaleb(-int(value["scale"]))


def read_raw_csv(path: Path) -> tuple[list[str], list[dict[str, str]], list[bytes]]:
    payload = path.read_bytes()
    if not payload.startswith(b"\xef\xbb\xbf"):
        fail(f"CSV UTF-8 BOM missing: {path}")
    body = payload[3:]
    if not body.endswith(b"\r\n"):
        fail(f"CSV final CRLF missing: {path}")
    if re.search(rb"(?<!\r)\n|\r(?!\n)", body):
        fail(f"CSV is not CRLF-only: {path}")
    if b'"' in body:
        fail(f"Golden raw sample unexpectedly contains quote characters: {path}")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        fail(f"CSV is not valid UTF-8: {path}: {error}")
    lines = body.split(b"\r\n")
    data_row_bytes = lines[1:-1]
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = reader.fieldnames or []
        rows = list(reader)
    except csv.Error as error:
        fail(f"CSV parse failure: {path}: {error}")
    if len(rows) != len(data_row_bytes):
        fail(f"CSV physical/logical row count differs: {path}")
    return headers, rows, data_row_bytes


def local_to_utc(value: str, timezone_name: str) -> str:
    local = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(timezone_name))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def compute_coverage(role: str, rows: list[dict[str, str]], timezone_name: str) -> dict[str, Any]:
    if role == "TRADE_HISTORY":
        values = [local_to_utc(row["Time"], timezone_name) for row in rows]
    else:
        values = ([min(local_to_utc(row["Opened"], timezone_name) for row in rows)] if rows else [])
        values += ([max(local_to_utc(row["Closed"], timezone_name) for row in rows)] if rows else [])
    return {
        "artifactRole": role,
        "coverageCompleteness": "NOT_PROVEN",
        "earliestObservedAt": min(values) if values else None,
        "fixtureCompletenessScope": "SYNTHETIC_SCENARIO_ONLY",
        "latestObservedAt": max(values) if values else None,
        "rowCount": len(rows),
        "sourceTimezone": timezone_name,
        "timestampPrecision": "SECOND",
        "venueExportCompletenessAsserted": False,
    }


def compute_common_interval(coverages: list[dict[str, Any]]) -> dict[str, str] | None:
    by_role = {coverage["artifactRole"]: coverage for coverage in coverages}
    trade = by_role["TRADE_HISTORY"]
    position = by_role["POSITION_HISTORY"]
    if not all([trade["earliestObservedAt"], trade["latestObservedAt"], position["earliestObservedAt"], position["latestObservedAt"]]):
        return None
    start = max(trade["earliestObservedAt"], position["earliestObservedAt"])
    end = min(trade["latestObservedAt"], position["latestObservedAt"])
    return {"startInclusive": start, "endInclusive": end} if start <= end else None


def validate_schema(value: Any, schema: Any, path: Path) -> None:
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(value), key=lambda error: list(error.absolute_path))
    if errors:
        error = errors[0]
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        fail(f"Schema validation failed {path} at {location}: {error.message}")


def validate_headers_and_lexemes(fixture_id: str, manifest: dict[str, Any], trade_headers: list[str], trade_rows: list[dict[str, str]], position_headers: list[str], position_rows: list[dict[str, str]]) -> None:
    failure_code = manifest["expectedFailureCode"]
    if fixture_id == "required-column-missing":
        if set(TRADE_HEADERS) - set(trade_headers) != {"Maker"}:
            fail(f"Required-column negative fixture drift: {fixture_id}")
    elif fixture_id == "unsupported-source-schema":
        if "Trade Id" not in trade_headers or "Trade ID" in trade_headers:
            fail(f"Unsupported-schema negative fixture drift: {fixture_id}")
    elif trade_headers != TRADE_HEADERS:
        fail(f"Trade exact header mismatch: {fixture_id}")
    if position_headers != POSITION_HEADERS:
        fail(f"Position exact header mismatch: {fixture_id}")
    if len(trade_headers) != len(set(trade_headers)) or len(position_headers) != len(set(position_headers)):
        fail(f"Duplicate CSV header: {fixture_id}")
    for number, row in enumerate(trade_rows, 1):
        if not TIMESTAMP.fullmatch(row.get("Time", "")):
            fail(f"Trade timestamp is not exact second precision: {fixture_id} row {number}")
        for field in ["Buyer", "Maker"]:
            if field in row and row[field] not in {"true", "false"}:
                fail(f"Trade Boolean lexical format mismatch: {fixture_id} row {number} {field}")
        if row.get("Side") not in {"BUY", "SELL"}:
            fail(f"Trade Side lexical format mismatch: {fixture_id} row {number}")
        trade_id = row.get("Trade ID", row.get("Trade Id", ""))
        if fixture_id != "invalid-trade-id" and not re.fullmatch(r"83\d{10}", trade_id):
            fail(f"Synthetic Trade ID namespace or numeric format violation: {fixture_id} row {number}")
        if not re.fullmatch(r"84\d{10}", row.get("Order ID", "")):
            fail(f"Synthetic Order ID namespace violation: {fixture_id} row {number}")
        if not re.fullmatch(r"88\d{10}", row.get("Uid", "")):
            fail(f"Synthetic UID namespace violation: {fixture_id} row {number}")
        for field in ["Price", "Quantity", "Amount", "Realized Profit"]:
            if fixture_id == "invalid-numeric-value" and field == "Quantity":
                continue
            if not DECIMAL.fullmatch(row.get(field, "")):
                fail(f"Trade Decimal lexical format mismatch: {fixture_id} row {number} {field}")
    for number, row in enumerate(position_rows, 1):
        for field in ["Opened", "Closed"]:
            if not TIMESTAMP.fullmatch(row.get(field, "")):
                fail(f"Position timestamp is not exact second precision: {fixture_id} row {number} {field}")
        for field in ["Entry Price", "Avg. Close Price", "Max Open Interest", "Closed Vol.", "Closing PNL"]:
            if not DECIMAL.fullmatch(row.get(field, "")):
                fail(f"Position Decimal lexical format mismatch: {fixture_id} row {number} {field}")
    if failure_code is None and fixture_id not in {"duplicate-execution-same-content"}:
        identities = [(row["Symbol"], row.get("Trade ID", "")) for row in trade_rows]
        if len(identities) != len(set(identities)):
            fail(f"Unexpected duplicate Trade ID in accepted fixture: {fixture_id}")


def validate_provenance(refs: list[dict[str, Any]], row_bytes: dict[str, list[bytes]], label: str) -> None:
    if not refs:
        fail(f"Missing source provenance: {label}")
    for ref in refs:
        role = ref["artifactRole"]
        number = ref["sourceRowNumber"]
        if role not in row_bytes or number < 1 or number > len(row_bytes[role]):
            fail(f"Out-of-range source row provenance: {label}")
        if ref["sourceRowSha256"] != digest(row_bytes[role][number - 1]):
            fail(f"Source row hash mismatch: {label}")


def validate_record_integrity(canonical: dict[str, Any], reconciliation: dict[str, Any], row_bytes: dict[str, list[bytes]]) -> None:
    snapshots = {snapshot["id"]: snapshot for snapshot in canonical["sourceEvidenceSnapshots"]}
    expected_snapshot_count = sum(len(rows) for rows in row_bytes.values())
    if len(snapshots) != expected_snapshot_count:
        fail("Source evidence snapshot count mismatch")
    for snapshot in snapshots.values():
        role = snapshot["artifactRole"]
        number = snapshot["sourceRowNumber"]
        if snapshot["sourceRowHash"] != digest(row_bytes[role][number - 1]):
            fail(f"Source evidence row hash mismatch: {snapshot['id']}")
    records: dict[str, dict[str, Any]] = {}
    for record in canonical["records"]:
        record_id = record["recordId"]
        if record_id in records:
            fail(f"Duplicate canonical record ID: {record_id}")
        records[record_id] = record
        copied = dict(record)
        recorded_hash = copied.pop("recordHash")
        if recorded_hash != digest(canonical_bytes(copied)):
            fail(f"Canonical record hash mismatch: {record_id}")
        validate_provenance(record["sourceProvenance"], row_bytes, record_id)
        if any(snapshot_id not in snapshots for snapshot_id in record["sourceEvidenceSnapshotIds"]):
            fail(f"Canonical record links missing source evidence: {record_id}")
        if {snapshots[item]["sourceRowHash"] for item in record["sourceEvidenceSnapshotIds"]} != {ref["sourceRowSha256"] for ref in record["sourceProvenance"]}:
            fail(f"Canonical/source evidence hash disagreement: {record_id}")

    units = {unit["reviewUnitId"]: unit for unit in reconciliation["reviewUnits"]}
    allocations = reconciliation["allocations"]
    for unit in units.values():
        validate_provenance(unit["sourceProvenance"], row_bytes, unit["reviewUnitId"])
        embedded = [item["recordId"] for item in unit["allocations"]]
        top_level = [item["recordId"] for item in allocations if item["reviewUnitId"] == unit["reviewUnitId"]]
        if embedded != top_level:
            fail(f"Embedded allocation drift: {unit['reviewUnitId']}")
        if unit["eligible"]:
            if unit["reconstructionStatus"] != "COMPLETE" or unit["reconciliationStatus"] not in {"EXACT", "WITHIN_ROUNDING_TOLERANCE"} or unit["exclusionReasons"]:
                fail(f"Invalid eligible ReviewUnit state: {unit['reviewUnitId']}")
        elif not unit["exclusionReasons"]:
            fail(f"Excluded ReviewUnit has no exclusion reason: {unit['reviewUnitId']}")
        if "CLOSED_OUTCOME" in unit["capability"]:
            gross = decimal_value(unit["grossTradingPnl"])
            fee = decimal_value(unit["tradingFee"])
            if decimal_value(unit["netTradingPnl"]) != gross - fee:
                fail(f"gross-fee-net relation mismatch: {unit['reviewUnitId']}")
        elif unit.get("netTradingPnl") is not None:
            fail(f"Unavailable net outcome represented as a value: {unit['reviewUnitId']}")

    for allocation in allocations:
        if allocation["reviewUnitId"] not in units:
            fail(f"Allocation links unknown ReviewUnit: {allocation['reviewUnitId']}")
        required_records = [allocation["recordId"], allocation["feeRecordId"], allocation["reportedPnlRecordId"]]
        expected_types = ["EXECUTION", "FEE", "VENUE_REPORTED_PNL"]
        if any(record_id not in records for record_id in required_records):
            fail(f"Allocation links unknown canonical record: {allocation['recordId']}")
        if [records[record_id]["recordType"] for record_id in required_records] != expected_types:
            fail(f"Allocation record type mismatch: {allocation['recordId']}")
        validate_provenance(allocation["sourceProvenance"], row_bytes, allocation["recordId"])
        if allocation["action"] in {"OPEN", "INCREASE", "REVERSAL_OPEN"} and decimal_value(allocation["allocatedReportedPnl"]) != 0:
            fail(f"Opening allocation has non-zero reported PnL: {allocation['recordId']}")

    links = reconciliation["evidenceLinks"]
    if len(links) != len(allocations):
        fail("Expected evidence link missing")
    linked_allocations = {link["allocationRecordId"] for link in links}
    if linked_allocations != {allocation["recordId"] for allocation in allocations}:
        fail("Expected evidence link does not cover every allocation")
    for link in links:
        if any(record_id not in records for record_id in link["canonicalRecordIds"]):
            fail("Evidence link references unknown canonical record")
        if any(snapshot_id not in snapshots for snapshot_id in link["sourceEvidenceSnapshotIds"]):
            fail("Evidence link references unknown source evidence")


def validate_conservation(canonical: dict[str, Any], reconciliation: dict[str, Any]) -> None:
    if not reconciliation["importResult"]["accepted"]:
        return
    records = {record["recordId"]: record for record in canonical["records"]}
    quantities: defaultdict[str, Decimal] = defaultdict(Decimal)
    fees: defaultdict[tuple[str, str], Decimal] = defaultdict(Decimal)
    pnls: defaultdict[str, Decimal] = defaultdict(Decimal)
    for allocation in reconciliation["allocations"]:
        quantities[allocation["recordId"]] += decimal_value(allocation["allocatedQuantity"])
        for asset, value in allocation["allocatedFeeByAsset"].items():
            fees[(allocation["feeRecordId"], asset)] += decimal_value(value)
        pnls[allocation["reportedPnlRecordId"]] += decimal_value(allocation["allocatedReportedPnl"])
    for record in records.values():
        if record["recordType"] == "EXECUTION" and quantities[record["recordId"]] != decimal_value(record["quantity"]):
            fail(f"Static quantity conservation mismatch: {record['recordId']}")
        if record["recordType"] == "FEE":
            amount = record["sourceAmount"]
            if fees[(record["recordId"], amount["asset"])] != decimal_value(amount["amount"]):
                fail(f"Static fee conservation mismatch: {record['recordId']}")
        if record["recordType"] == "VENUE_REPORTED_PNL":
            if pnls[record["recordId"]] != decimal_value(record["realizedPnl"]["amount"]):
                fail(f"Static PnL conservation mismatch: {record['recordId']}")


def validate_fixture(root: Path, entry: dict[str, Any], schemas: dict[str, Any]) -> None:
    fixture_id = entry["fixtureId"]
    folder = root / entry["path"]
    if folder.name != fixture_id:
        fail(f"Fixture path/id mismatch: {fixture_id}")
    manifest = load_json(folder / "fixture-manifest.json")
    canonical = load_json(folder / "expected/canonical-records.json")
    reconciliation = load_json(folder / "expected/reconciliation-manifest.json")
    quality = load_json(folder / "expected/quality-report.json")
    validate_schema(manifest, schemas["fixture-manifest"], folder / "fixture-manifest.json")
    validate_schema(canonical, schemas["canonical-records"], folder / "expected/canonical-records.json")
    validate_schema(reconciliation, schemas["reconciliation-manifest"], folder / "expected/reconciliation-manifest.json")
    validate_schema(quality, schemas["quality-report"], folder / "expected/quality-report.json")
    if any(value["fixtureId"] != fixture_id for value in [manifest, canonical, reconciliation, quality]):
        fail(f"Fixture identity mismatch inside expected JSON: {fixture_id}")
    if manifest["contextScenarioIds"] != entry["contextScenarioIds"] or manifest["acceptanceScenarioIds"] != entry["acceptanceScenarioIds"]:
        fail(f"Fixture index/manifest traceability mismatch: {fixture_id}")

    trade_headers, trade_rows, trade_row_bytes = read_raw_csv(folder / "trade-history.csv")
    position_headers, position_rows, position_row_bytes = read_raw_csv(folder / "position-history.csv")
    validate_headers_and_lexemes(fixture_id, manifest, trade_headers, trade_rows, position_headers, position_rows)
    row_bytes = {"TRADE_HISTORY": trade_row_bytes, "POSITION_HISTORY": position_row_bytes}
    coverages = [compute_coverage("TRADE_HISTORY", trade_rows, manifest["sourceTimezone"]), compute_coverage("POSITION_HISTORY", position_rows, manifest["sourceTimezone"])]
    if manifest["artifactCoverages"] != coverages or canonical["artifactCoverages"] != coverages or reconciliation["artifactCoverages"] != coverages or quality["artifactCoverages"] != coverages:
        fail(f"Artifact coverage mismatch: {fixture_id}")
    common = compute_common_interval(coverages)
    if any(value["commonAnalyzableInterval"] != common for value in [manifest, reconciliation, quality]):
        fail(f"Common analyzable interval mismatch: {fixture_id}")
    if quality["rowCountsByArtifactRole"] != {"TRADE_HISTORY": len(trade_rows), "POSITION_HISTORY": len(position_rows)}:
        fail(f"Quality row count mismatch: {fixture_id}")

    descriptors = {descriptor["sourceSymbol"]: descriptor["instrumentId"] for descriptor in manifest["instrumentDescriptors"]}
    raw_symbols = {row["Symbol"] for row in trade_rows + position_rows}
    if any(not re.search(r"RV3F\d{2}_.*SYNTH", symbol) for symbol in raw_symbols):
        fail(f"Synthetic source symbol namespace violation: {fixture_id}")
    if set(descriptors) != raw_symbols:
        fail(f"Pinned instrument descriptor lookup mismatch: {fixture_id}")
    for record in canonical["records"]:
        if record["venueSymbol"] not in descriptors or record["instrumentId"] != descriptors[record["venueSymbol"]]:
            fail(f"Canonical source symbol is not losslessly pinned: {record['recordId']}")
        if record["recordType"] == "EXECUTION" and (not re.fullmatch(r"83\d{10}", record["rawTradeId"]) or not re.fullmatch(r"84\d{10}", record["rawOrderId"])):
            fail(f"Canonical synthetic execution ID namespace violation: {record['recordId']}")
    if fixture_id == "unicode-symbol":
        symbol = next(iter(raw_symbols))
        if symbol == unicodedata.normalize("NFC", symbol) or "Å" not in symbol:
            fail("Unicode symbol normalization sentinel drift")
        if any(record["venueSymbol"] != symbol for record in canonical["records"]):
            fail("Unicode source symbol was normalized or changed")

    validate_record_integrity(canonical, reconciliation, row_bytes)
    validate_conservation(canonical, reconciliation)

    executions = [record for record in canonical["records"] if record["recordType"] == "EXECUTION"]
    expected_order = sorted(executions, key=lambda record: (record["instrumentId"], record["occurredAt"], int(record["rawTradeId"]))) if all(record["rawTradeId"].isdigit() for record in executions) else []
    if set(reconciliation["orderedExecutionKeysByInstrument"]) != {record["instrumentId"] for record in executions}:
        fail(f"Canonical execution partition key mismatch: {fixture_id}")
    flattened_order = [key for instrument in sorted(reconciliation["orderedExecutionKeysByInstrument"]) for key in reconciliation["orderedExecutionKeysByInstrument"][instrument]]
    if expected_order and flattened_order != [record["sourceExecutionKey"] for record in expected_order]:
        fail(f"Canonical execution order mismatch: {fixture_id}")
    if fixture_id == "source-order-scrambled":
        raw_order = [row["Trade ID"] for row in trade_rows]
        canonical_order = [record["rawTradeId"] for record in expected_order]
        if raw_order == canonical_order:
            fail("Source-order-scrambled no longer differs from canonical order")
        for record in executions:
            row = record["sourceProvenance"][0]["sourceRowNumber"]
            if trade_rows[row - 1]["Trade ID"] != record["rawTradeId"]:
                fail("Scrambled source provenance row number was not preserved")
    if fixture_id == "second-precision-position":
        if not position_rows or any(row["Opened"].endswith(":00") or row["Closed"].endswith(":00") for row in position_rows):
            fail("Second-precision Position sentinel was reduced to minute precision")
        summary_records = [record for record in canonical["records"] if record["recordType"] == "VENUE_POSITION_SUMMARY"]
        for row, record in zip(position_rows, summary_records, strict=True):
            if record["openedAt"] != local_to_utc(row["Opened"], manifest["sourceTimezone"]) or record["closedAt"] != local_to_utc(row["Closed"], manifest["sourceTimezone"]):
                fail("Position exact-second timestamp was not canonicalized to the exact UTC instant")
    if fixture_id == "multiple-fills-same-order":
        if len({row["Order ID"] for row in trade_rows}) != 1 or len({row["Trade ID"] for row in trade_rows}) != len(trade_rows):
            fail("Same-order fills were merged or fixture drifted")
        if len(executions) != len(trade_rows):
            fail("Same-order fills are not preserved as individual executions")
    if fixture_id == "closing-pnl-excludes-fee":
        unit = reconciliation["reviewUnits"][0]
        summary_id = unit["venuePositionSummaryRecordIds"][0]
        summary = next(record for record in canonical["records"] if record["recordId"] == summary_id)
        if decimal_value(summary["closingPnl"]["amount"]) != decimal_value(unit["grossTradingPnl"]):
            fail("Position Closing PNL no longer equals closing-fill gross realized PnL")
        if decimal_value(unit["tradingFee"]) == 0 or decimal_value(unit["grossTradingPnl"]) == decimal_value(unit["netTradingPnl"]):
            fail("Closing-PNL fixture no longer distinguishes gross from fee/net")
    if fixture_id == "left-censored-before-trade-coverage":
        by_role = {coverage["artifactRole"]: coverage for coverage in coverages}
        if by_role["POSITION_HISTORY"]["earliestObservedAt"] >= by_role["TRADE_HISTORY"]["earliestObservedAt"]:
            fail("Left-censored Position no longer begins before Trade coverage")
        if not any(unit["reconstructionStatus"] == "LEFT_CENSORED" and unit["reconciliationStatus"] == "INCOMPLETE_TRADES" and not unit["eligible"] and unit["initialQuantity"] is None for unit in reconciliation["reviewUnits"]):
            fail("Left-censored unknown opening state was inferred or made eligible")
    if fixture_id == "episode-after-position-coverage":
        position_latest = next(coverage["latestObservedAt"] for coverage in coverages if coverage["artifactRole"] == "POSITION_HISTORY")
        candidates = [unit for unit in reconciliation["reviewUnits"] if "POSITION_SUMMARY_MISSING" in unit["exclusionReasons"]]
        if position_latest is None or len(candidates) != 1:
            fail("After-coverage fixture lacks independent Position coverage or missing-summary episode")
        unit = candidates[0]
        if unit["openedAt"] <= position_latest or unit["reconstructionStatus"] != "COMPLETE" or unit["reconciliationStatus"] != "SUMMARY_MISMATCH" or unit["eligible"]:
            fail("Episode after Position coverage was treated as exact/empty instead of excluded mismatch")
    if fixture_id == "common-window-exact-reconciliation":
        units = [unit for unit in reconciliation["reviewUnits"] if unit["reconciliationStatus"] == "EXACT" and unit["eligible"]]
        if len(units) != 1 or common is None:
            fail("Common-window exact fixture lost its unique eligible reconciliation")
        unit = units[0]
        summary = next(record for record in canonical["records"] if record["recordId"] == unit["venuePositionSummaryRecordIds"][0])
        average_entry = decimal_value(unit["averageEntryPrice"]["numerator"]) / decimal_value(unit["averageEntryPrice"]["denominator"])
        average_close = decimal_value(unit["averageExitPrice"]["numerator"]) / decimal_value(unit["averageExitPrice"]["denominator"])
        checks = [
            unit["openedAt"] == summary["openedAt"],
            unit["closedAt"] == summary["closedAt"],
            decimal_value(unit["maxQuantity"]) == decimal_value(summary["maxOpenInterest"]),
            decimal_value(unit["closingVolume"]) == decimal_value(summary["closedVolume"]),
            average_entry == decimal_value(summary["entryPrice"]),
            average_close == decimal_value(summary["averageClosePrice"]),
            decimal_value(unit["grossTradingPnl"]) == decimal_value(summary["closingPnl"]["amount"]),
        ]
        if not all(checks):
            fail("Common-window exact reconciliation field mismatch")
    if fixture_id == "exact-timestamp-non-unique-match":
        if not reconciliation["reviewUnits"] or any(unit["reconciliationStatus"] != "AMBIGUOUS" or unit["eligible"] for unit in reconciliation["reviewUnits"]):
            fail("Exact-timestamp non-unique candidates were arbitrarily matched")

    canonical_hash = digest(canonical_bytes(canonical))
    if reconciliation["canonicalRecordSetHash"] != canonical_hash:
        fail(f"Canonical record set hash mismatch: {fixture_id}")
    version_payload = dict(manifest["versionMetadata"])
    recorded_version_hash = version_payload.pop("versionSetHash")
    if recorded_version_hash != digest(canonical_bytes(version_payload)) or reconciliation["versionSetHash"] != recorded_version_hash:
        fail(f"Version set hash mismatch: {fixture_id}")
    manifest_payload = dict(reconciliation)
    recorded_manifest_hash = manifest_payload.pop("manifestHash")
    if recorded_manifest_hash != digest(canonical_bytes(manifest_payload)):
        fail(f"Reconciliation manifest hash mismatch: {fixture_id}")
    for artifact in manifest["artifacts"]:
        path = folder / artifact["path"]
        if artifact["sha256"] != digest_file(path) or artifact["sizeBytes"] != path.stat().st_size:
            fail(f"Source artifact checksum/size mismatch: {fixture_id}/{artifact['path']}")
        if reconciliation["sourceArtifactContentHashes"][artifact["role"]] != artifact["sha256"]:
            fail(f"Source artifact manifest hash mismatch: {fixture_id}/{artifact['role']}")

    expected_files = {"trade-history.csv", "position-history.csv", "fixture-manifest.json", *REQUIRED_EXPECTED}
    if manifest["expectedImportStatus"] == "REJECTED":
        expected_files.add("expected/failure.json")
        failure_path = folder / "expected/failure.json"
        failure_value = load_json(failure_path)
        validate_schema(failure_value, schemas["expected-failure"], failure_path)
        if failure_value["failureCode"] != manifest["expectedFailureCode"]:
            fail(f"Expected failure code mismatch: {fixture_id}")
    indexed = {item["path"]: item for item in entry["files"]}
    if set(indexed) != expected_files:
        fail(f"Fixture index file catalog mismatch: {fixture_id}")
    for relative, metadata in indexed.items():
        path = folder / relative
        if metadata["sha256"] != digest_file(path) or metadata["sizeBytes"] != path.stat().st_size:
            fail(f"Fixture index checksum/size mismatch: {fixture_id}/{relative}")


def validate_v1_tree(root: Path, expected: dict[str, Any]) -> None:
    source = (root / expected["path"]).resolve()
    if not source.is_dir():
        fail(f"Preserved v1 fixture root missing: {source}")
    entries = []
    for path in sorted(source.rglob("*")):
        if path.is_file():
            entries.append(f"{path.relative_to(source).as_posix()}:{digest_file(path)}")
    tree_hash = digest("\n".join(entries).encode())
    if tree_hash != expected["treeHash"]:
        fail("Preserved binance-usds-v1 tree hash mismatch")


def validate_raw_v3(root: Path, context_path: Path, *, check_links: bool = True) -> int:
    if not root.is_dir():
        fail(f"Fixture root does not exist: {root}")
    index = load_json(root / "fixture-index.json")
    if index.get("fixtureSetVersion") != 3:
        fail("raw-v3 fixtureSetVersion must be 3")
    contract = index["contextContract"]
    if contract["reconstructionPolicyVersion"] != POLICY or contract["sourceDialectVersionsByArtifactRole"] != {"TRADE_HISTORY": TRADE_DIALECT, "POSITION_HISTORY": POSITION_DIALECT}:
        fail("Product V1 identifiers drifted in raw-v3 index")
    for relative, tokens in {
        "docs/TRADING_REVIEW.md": [TRADE_DIALECT, POSITION_DIALECT, "coverageCompleteness: NOT_PROVEN"],
        "docs/TRADING_RECONSTRUCTION.md": [POLICY, "## O. Scenario Tables", "POSITION_SUMMARY_MISSING"],
        "docs/TRADING_REVIEW_ACCEPTANCE.md": ["TR-ACC-RECON-008", "TR-ACC-RECON-012"],
        "docs/TRADING_VERSIONING.md": ["adapterVersion", "targetVersionSetHash"],
        "docs/DECISIONS.md": ["ADR-058", "ADR-059"],
        "openapi/core-api.yaml": ["ArtifactCoverage", "evidenceSchemaVersion"],
        "openapi/compute-api.yaml": ["InternalArtifactCoverage", "CanonicalPositionSummaryRecord"],
    }.items():
        text = (context_path / relative).read_text(encoding="utf-8")
        for token in tokens:
            if token not in text:
                fail(f"Context contract token {token!r} missing from {relative}")
    if check_links:
        readme = (root / "README.md").read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", readme):
            if "://" not in target and not target.startswith("#") and not (root / target.split("#", 1)[0]).resolve().exists():
                fail(f"Broken README link: {target}")

    schemas = {}
    for name in ["fixture-manifest", "canonical-records", "reconciliation-manifest", "quality-report", "expected-failure"]:
        schema = load_json(root / "schemas" / f"{name}.schema.json")
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as error:
            fail(f"Invalid raw-v3 schema {name}: {error.message}")
        schemas[name] = schema
    entries = index["fixtures"]
    fixture_ids = [entry["fixtureId"] for entry in entries]
    if len(fixture_ids) != len(set(fixture_ids)):
        fail("Duplicate raw-v3 fixture ID")
    if not SPECIAL_FIXTURES.issubset(fixture_ids):
        fail(f"Required raw contract fixtures missing: {sorted(SPECIAL_FIXTURES - set(fixture_ids))}")
    scenarios = index["contextScenarios"]
    scenario_mapping = {item["contextScenarioId"]: item["fixtureId"] for item in scenarios}
    if len(scenarios) != 25 or scenario_mapping != EXPECTED_CONTEXT_SCENARIOS:
        fail("raw-v3 Context traceability must match all 25 current reconstruction scenarios")
    validate_v1_tree(root, index["preservedV1"])
    for entry in entries:
        validate_fixture(root, entry, schemas)
    source_order_manifest = load_json(root / "source-order-scrambled/expected/reconciliation-manifest.json")
    ordered_manifest = load_json(root / "partial-entry/expected/reconciliation-manifest.json")
    def economic_signature(value: dict[str, Any]) -> Any:
        units = []
        for unit in value["reviewUnits"]:
            units.append({
                "actions": [(item["action"], item["allocatedQuantity"], item["allocatedReportedPnl"], item["allocatedFeeByAsset"]) for item in unit["allocations"]],
                "averageEntryPrice": unit["averageEntryPrice"],
                "averageExitPrice": unit["averageExitPrice"],
                "closingVolume": unit["closingVolume"],
                "direction": unit["direction"],
                "grossTradingPnl": unit["grossTradingPnl"],
                "initialQuantity": unit["initialQuantity"],
                "maxQuantity": unit["maxQuantity"],
                "netTradingPnl": unit["netTradingPnl"],
                "openingVolume": unit["openingVolume"],
                "reconciliationStatus": unit["reconciliationStatus"],
                "reconstructionStatus": unit["reconstructionStatus"],
                "tradingFeesByAsset": unit["tradingFeesByAsset"],
            })
        return units
    if economic_signature(source_order_manifest) != economic_signature(ordered_manifest):
        fail("Scrambled input row order changed the authored economic manifest meaning")
    indexed_package = {item["path"]: item for item in index["packageFiles"]}
    expected_package = {"README.md", *{f"schemas/{name}.schema.json" for name in schemas}}
    if set(indexed_package) != expected_package:
        fail("raw-v3 package checksum catalog mismatch")
    for relative, metadata in indexed_package.items():
        path = root / relative
        if metadata["sha256"] != digest_file(path) or metadata["sizeBytes"] != path.stat().st_size:
            fail(f"raw-v3 package checksum mismatch: {relative}")
    payload = b"\n".join(path.read_bytes() for path in root.rglob("*") if path.is_file())
    text = payload.decode("utf-8", errors="ignore")
    for pattern in [r"(?i)(api[_-]?key|password|credential)", r"(?i)[A-Z]:\\", r"(?i)originalFilename|storageReference"]:
        if re.search(pattern, text):
            fail(f"Sensitive identifier/path pattern found: {pattern}")
    return len(entries)


def tamper_self_test_raw_v3(root: Path, context_path: Path) -> list[str]:
    cases: list[tuple[str, Callable[[Path], None]]] = []

    def bytes_mutator(relative: str, transform: Callable[[bytes], bytes]) -> Callable[[Path], None]:
        def mutate(copied: Path) -> None:
            path = copied / relative
            path.write_bytes(transform(path.read_bytes()))
        return mutate

    cases.extend([
        ("bom-removed", bytes_mutator("raw-byte-dialect/trade-history.csv", lambda data: data[3:])),
        ("crlf-to-lf", bytes_mutator("raw-byte-dialect/trade-history.csv", lambda data: data.replace(b"\r\n", b"\n"))),
        ("header-case", bytes_mutator("raw-byte-dialect/trade-history.csv", lambda data: data.replace(b"Uid,Time", b"UID,Time", 1))),
        ("timestamp-seconds-removed", bytes_mutator("raw-byte-dialect/trade-history.csv", lambda data: data.replace(b"10:00:01", b"10:00", 1))),
        ("trade-id-duplicate", bytes_mutator("raw-byte-dialect/trade-history.csv", lambda data: data.replace(b"833100000102", b"833100000101"))),
        ("unicode-normalized", bytes_mutator("unicode-symbol/trade-history.csv", lambda data: unicodedata.normalize("NFC", data.decode("utf-8")).encode("utf-8"))),
    ])

    def mutate_json(relative: str, mutate: Callable[[dict[str, Any]], None]) -> Callable[[Path], None]:
        def apply(copied: Path) -> None:
            path = copied / relative
            value = json.loads(path.read_text(encoding="utf-8"))
            mutate(value)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        return apply

    cases.extend([
        ("source-row-hash", mutate_json("raw-byte-dialect/expected/canonical-records.json", lambda value: value["records"][0]["sourceProvenance"][0].__setitem__("sourceRowSha256", "0" * 64))),
        ("artifact-coverage", mutate_json("raw-byte-dialect/expected/quality-report.json", lambda value: value["artifactCoverages"][0].__setitem__("rowCount", 99))),
        ("manifest-hash", mutate_json("raw-byte-dialect/expected/reconciliation-manifest.json", lambda value: value.__setitem__("manifestHash", "0" * 64))),
        ("closing-pnl-netted", mutate_json("closing-pnl-excludes-fee/expected/canonical-records.json", lambda value: next(record for record in value["records"] if record["recordType"] == "VENUE_POSITION_SUMMARY")["closingPnl"].__setitem__("amount", {"scale": 2, "unscaled": "196"}))),
        ("evidence-link-removed", mutate_json("raw-byte-dialect/expected/reconciliation-manifest.json", lambda value: value["evidenceLinks"].pop())),
    ])

    passed = []
    for label, mutate in cases:
        with tempfile.TemporaryDirectory(prefix=f"refinvest-raw-v3-{label}-") as temp:
            copied = Path(temp) / root.name
            shutil.copytree(root, copied)
            mutate(copied)
            try:
                validate_raw_v3(copied, context_path, check_links=False)
            except RawV3ValidationFailure:
                passed.append(label)
                continue
            fail(f"Corruption self-test accepted mutation: {label}")
    return passed
