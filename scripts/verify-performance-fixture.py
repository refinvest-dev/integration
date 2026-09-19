"""Verify deterministic performance artifacts and all benchmark scenario inputs."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from performance_fixture import (
    ARTIFACT_NAMES,
    MVP_ASSETS,
    assemble_fixture,
    definition_sha256,
    load_json_object,
    load_with_compute,
    sha256_file,
    validate_scenarios,
)
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarSessionState,
    DailyBarAvailability,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--definition", type=Path, required=True)
    parser.add_argument("--fixture-manifest", type=Path, required=True)
    parser.add_argument("--scenario-directory", type=Path, required=True)
    parser.add_argument("--snapshot-path", type=Path, required=True)
    parser.add_argument("--compute-api-path", type=Path, required=True)
    args = parser.parse_args()

    definition = load_json_object(args.definition)
    manifest = load_json_object(args.fixture_manifest)
    assembly = assemble_fixture(definition, args.snapshot_path.resolve())
    _verify_metadata(definition, manifest, assembly, args.compute_api_path.resolve())
    _verify_artifact_hashes(manifest, args.snapshot_path.resolve())
    execution_data = load_with_compute(assembly)
    _verify_loaded_data(definition, manifest, execution_data)
    scenario_ids = validate_scenarios(args.scenario_directory, definition["fixtureId"])
    print(
        f"Verified {definition['fixtureId']}: {len(MVP_ASSETS)} assets, "
        f"{len(scenario_ids)} scenarios, Compute loader and artifact hashes OK."
    )


def _verify_metadata(definition, manifest, assembly, compute_api_path: Path) -> None:
    required_pairs = {
        "fixtureId": definition["fixtureId"],
        "fixtureDefinitionVersion": definition["fixtureDefinitionVersion"],
        "createdAt": definition["createdAt"],
        "period": definition["period"],
        "frequency": definition["frequency"],
        "assets": list(MVP_ASSETS),
        "adjustmentPolicy": definition["adjustmentPolicy"],
        "fixtureDefinitionSha256": definition_sha256(definition),
    }
    for field, expected in required_pairs.items():
        if manifest.get(field) != expected:
            raise ValueError(f"Fixture manifest {field} does not match the definition.")
    calendar = manifest.get("calendar", {})
    expected_calendar = {
        "source": assembly.snapshot.calendar_source,
        "version": assembly.snapshot.calendar_version,
        "contentHash": assembly.snapshot.calendar_hash,
    }
    for field, expected in expected_calendar.items():
        if calendar.get(field) != expected:
            raise ValueError(f"Fixture manifest calendar {field} does not match generated data.")
    generator = manifest.get("generator", {})
    if generator.get("computeDependencyLockSha256") != sha256_file(compute_api_path / "uv.lock"):
        raise ValueError("Compute dependency lock differs from the fixture provenance.")


def _verify_artifact_hashes(manifest, snapshot_path: Path) -> None:
    entries = manifest.get("artifacts")
    if not isinstance(entries, list):
        raise TypeError("Fixture manifest artifacts must be an array.")
    expected = {entry.get("name"): entry for entry in entries if isinstance(entry, dict)}
    if set(expected) != set(ARTIFACT_NAMES):
        raise ValueError("Fixture manifest must name exactly the Compute snapshot artifacts.")
    for name in ARTIFACT_NAMES:
        path = snapshot_path / name
        if sha256_file(path) != expected[name].get("sha256"):
            raise ValueError(f"Artifact hash mismatch: {name}")
        if path.stat().st_size != expected[name].get("sizeBytes"):
            raise ValueError(f"Artifact size mismatch: {name}")
    if manifest.get("storageIndependentManifestSha256") != sha256_file(
        snapshot_path / "manifest.json"
    ):
        raise ValueError("Storage-independent Compute manifest hash mismatch.")


def _verify_loaded_data(definition, manifest, execution_data) -> None:
    if execution_data.snapshot_id != definition["fixtureId"]:
        raise ValueError("Compute loader returned a different snapshot ID.")
    if execution_data.corporate_actions_applied is not definition["corporateActionsApplied"]:
        raise ValueError("corporateActionsApplied differs from the fixture definition.")
    if (
        execution_data.point_in_time_validation_passed
        is not definition["pointInTimeValidationPassed"]
    ):
        raise ValueError("pointInTimeValidationPassed differs from the fixture definition.")
    provenance = execution_data.calendar.provenance
    calendar_manifest = manifest["calendar"]
    if (provenance.source, provenance.version, provenance.content_hash) != (
        calendar_manifest["source"],
        calendar_manifest["version"],
        calendar_manifest["contentHash"],
    ):
        raise ValueError("Loaded calendar provenance differs from the fixture manifest.")

    counts = manifest["expectedSessionCounts"]
    for asset in MVP_ASSETS:
        sessions = execution_data.calendar.expected_sessions(asset)
        if len(sessions) != counts[asset] or not sessions:
            raise ValueError(f"Expected-session coverage mismatch: {asset}")
        for session in sessions:
            lookup = execution_data.market_data.get_daily_bar(asset, session)
            if lookup.availability is not DailyBarAvailability.AVAILABLE or lookup.bar is None:
                raise ValueError(f"Daily Bar coverage is incomplete: {asset} {session}")
            _verify_bar(lookup.bar)

    start = date.fromisoformat(definition["period"]["start"])
    end = date.fromisoformat(definition["period"]["end"])
    weekend_difference = False
    current = start
    while current <= end:
        if current.weekday() >= 5:
            btc_state = execution_data.calendar.session_state(AssetSession("BTCUSDT", current))
            qqq_state = execution_data.calendar.session_state(AssetSession("QQQ", current))
            if (
                btc_state is CalendarSessionState.EXPECTED
                and qqq_state is CalendarSessionState.NON_TRADING
            ):
                weekend_difference = True
                break
        current += timedelta(days=1)
    if not weekend_difference:
        raise ValueError("Fixture does not express a weekend crypto/equity calendar difference.")


def _verify_bar(bar) -> None:
    if min(bar.open, bar.high, bar.low, bar.close, bar.volume) <= Decimal(0):
        raise ValueError(f"Daily Bar values must be positive: {bar.asset} {bar.session_date}")
    if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
        raise ValueError(f"Daily Bar OHLC relationship is invalid: {bar.asset} {bar.session_date}")
    if bar.low > bar.high:
        raise ValueError(f"Daily Bar low exceeds high: {bar.asset} {bar.session_date}")


if __name__ == "__main__":
    main()
