"""Integration-owned orchestration for the deterministic performance fixture.

The Compute package remains the sole owner of calendar, snapshot artifact, loader,
and request-validation formats. This module supplies only synthetic values and
versioned integration metadata.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from refinvest.api.schemas.backtests import CreateBacktestRequestTransport
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.engine.validation import validate_strategy_version
from refinvest.ingestion.calendars.crypto_utc import build_crypto_utc_calendar
from refinvest.ingestion.calendars.us_equity import generate_us_equity_calendar
from refinvest.ingestion.snapshot.artifacts import FilesystemSnapshotArtifactPublisher
from refinvest.ingestion.snapshot.calendar import (
    CalendarComponent,
    composite_calendar_provenance,
)
from refinvest.marketdata.domain import (
    AssetCalendar,
    AssetSession,
    CalendarSession,
    DailyBar,
)
from refinvest.persistence.object_storage.snapshot_artifacts import (
    FilesystemSnapshotExecutionDataLoader,
)

MVP_ASSETS = ("QQQ", "SPY", "TQQQ", "SOXL", "BTCUSDT")
US_EQUITY_ASSETS = MVP_ASSETS[:-1]
ARTIFACT_NAMES = ("calendar.json", "daily-bars.ndjson", "manifest.json")
SCENARIO_IDS = {
    "simple-1y",
    "simple-10y",
    "cross-calendar-10y",
    "relative-multi-condition-10y",
    "dense-signals",
    "zero-trades",
}


@dataclass(frozen=True)
class FixtureAssembly:
    snapshot: DatasetSnapshot
    components: tuple[CalendarComponent, ...]
    sessions: dict[AssetSession, CalendarSession]
    bars: tuple[DailyBar, ...]


class NoOpMetadataWriter:
    """Keep pure artifact generation independent from PostgreSQL publication."""

    def create(self, snapshot: DatasetSnapshot) -> None:
        del snapshot


def load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON document must be an object: {path}")
    return cast(dict[str, Any], value)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def definition_sha256(definition: Mapping[str, object]) -> str:
    return sha256_bytes(canonical_json_bytes(definition))


def assemble_fixture(definition: Mapping[str, Any], storage_path: Path) -> FixtureAssembly:
    _validate_definition(definition)
    start = date.fromisoformat(definition["period"]["start"])
    end = date.fromisoformat(definition["period"]["end"])
    override_payload = definition["calendar"]["usEquity"]["officialOverrides"]
    overrides = {
        date.fromisoformat(day): (
            datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
            if value is not None
            else None
        )
        for day, value in override_payload.items()
    }
    us_equity = generate_us_equity_calendar(start, end, overrides)
    crypto_utc = build_crypto_utc_calendar(start, end)
    sessions = {**us_equity.sessions, **crypto_utc.sessions}
    components = (
        CalendarComponent(AssetCalendar.US_EQUITY, US_EQUITY_ASSETS, us_equity.provenance),
        CalendarComponent(AssetCalendar.CRYPTO_UTC, ("BTCUSDT",), crypto_utc.provenance),
    )
    provenance = composite_calendar_provenance(components, sessions)
    created_at = datetime.fromisoformat(definition["createdAt"].replace("Z", "+00:00"))
    snapshot = DatasetSnapshot(
        definition["fixtureId"],
        created_at.astimezone(UTC),
        definition["source"],
        {"firstDate": start.isoformat(), "lastDate": end.isoformat()},
        definition["adjustmentPolicy"],
        str(storage_path.resolve()),
        provenance.source,
        provenance.version,
        provenance.content_hash,
    )
    bars = _synthetic_bars(definition, sessions)
    return FixtureAssembly(snapshot, components, sessions, bars)


def generate_artifacts(definition: Mapping[str, Any], storage_path: Path) -> FixtureAssembly:
    assembly = assemble_fixture(definition, storage_path)
    FilesystemSnapshotArtifactPublisher().publish(
        assembly.snapshot,
        _top_level_provenance(assembly),
        assembly.sessions,
        assembly.bars,
        calendar_components=assembly.components,
        corporate_actions_applied=definition["corporateActionsApplied"],
        point_in_time_validation_passed=definition["pointInTimeValidationPassed"],
    )
    return assembly


def load_with_compute(assembly: FixtureAssembly):  # type annotation is owned by Compute
    return FilesystemSnapshotExecutionDataLoader().load(assembly.snapshot)


def derived_manifest(
    definition: Mapping[str, Any],
    assembly: FixtureAssembly,
    compute_api_path: Path,
) -> dict[str, object]:
    root = Path(assembly.snapshot.storage_path)
    loader_data = load_with_compute(assembly)
    component_metadata = [
        {
            "calendar": component.calendar.value,
            "assets": list(component.assets),
            "source": component.provenance.source,
            "version": component.provenance.version,
            "contentHash": component.provenance.content_hash,
        }
        for component in assembly.components
    ]
    artifacts = [
        {
            "name": name,
            "sha256": sha256_file(root / name),
            "sizeBytes": (root / name).stat().st_size,
        }
        for name in ARTIFACT_NAMES
    ]
    return {
        "schemaVersion": 1,
        "fixtureId": definition["fixtureId"],
        "fixtureDefinitionVersion": definition["fixtureDefinitionVersion"],
        "fixtureDefinitionSha256": definition_sha256(definition),
        "createdAt": definition["createdAt"],
        "period": definition["period"],
        "frequency": definition["frequency"],
        "assets": list(MVP_ASSETS),
        "expectedSessionCounts": {
            asset: len(loader_data.calendar.expected_sessions(asset)) for asset in MVP_ASSETS
        },
        "adjustmentPolicy": definition["adjustmentPolicy"],
        "calendar": {
            "source": assembly.snapshot.calendar_source,
            "version": assembly.snapshot.calendar_version,
            "contentHash": assembly.snapshot.calendar_hash,
            "components": component_metadata,
        },
        "artifacts": artifacts,
        "storageIndependentManifestSha256": sha256_file(root / "manifest.json"),
        "generator": {
            "name": definition["generator"]["name"],
            "version": definition["generator"]["version"],
            "computeRevision": _git_revision(compute_api_path),
            "computeDependencyLock": "uv.lock",
            "computeDependencyLockSha256": sha256_file(compute_api_path / "uv.lock"),
        },
    }


def validate_scenarios(scenario_directory: Path, fixture_id: str) -> list[str]:
    scenario_ids: list[str] = []
    paths = sorted(scenario_directory.glob("*.json"))
    if len(paths) != 6:
        raise ValueError("Exactly six benchmark scenario JSON files are required.")
    for path in paths:
        scenario = load_json_object(path)
        scenario_id = scenario.get("scenarioId")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ValueError(f"scenarioId is required: {path.name}")
        if scenario_id in scenario_ids:
            raise ValueError(f"Duplicate scenarioId: {scenario_id}")
        scenario_ids.append(scenario_id)
        if scenario.get("fixtureId") != fixture_id:
            raise ValueError(f"Scenario references a different fixture: {path.name}")
        if scenario.get("canonicalizationVersion") != 1:
            raise ValueError(f"Unsupported canonicalizationVersion: {path.name}")
        if scenario.get("scenarioVersion") != 1:
            raise ValueError(f"Unsupported scenarioVersion: {path.name}")
        if scenario.get("cacheModes") != ["COLD", "WARM"]:
            raise ValueError(f"Scenario must target separate COLD and WARM series: {path.name}")
        if scenario.get("expectedTerminalStatus") != "COMPLETED":
            raise ValueError(f"Scenario must expect COMPLETED: {path.name}")
        if "expectedResultHash" in scenario or "expectedTradeCount" in scenario:
            raise ValueError(f"Unmeasured result expectations are forbidden: {path.name}")
        request_payload = scenario.get("request")
        if not isinstance(request_payload, dict):
            raise TypeError(f"request must be an object: {path.name}")
        request = CreateBacktestRequestTransport.model_validate(request_payload)
        domain_request = request.to_domain()
        validate_strategy_version(domain_request.strategy_version)
        if domain_request.dataset_snapshot_id != fixture_id:
            raise ValueError(f"Scenario request references a different fixture: {path.name}")
        if domain_request.period.start > domain_request.period.end:
            raise ValueError(f"Scenario period is reversed: {path.name}")
    if set(scenario_ids) != SCENARIO_IDS:
        raise ValueError("Benchmark scenario IDs do not match the required version 1 set.")
    return scenario_ids


def _synthetic_bars(
    definition: Mapping[str, Any], sessions: Mapping[AssetSession, CalendarSession]
) -> tuple[DailyBar, ...]:
    generator = definition["generator"]
    regime_basis_points = generator["regimeBasisPoints"]
    result: list[DailyBar] = []
    asset_rules = {rule["symbol"]: rule for rule in generator["assets"]}
    for asset in MVP_ASSETS:
        rule = asset_rules[asset]
        expected_dates = sorted(
            asset_session.session_date
            for asset_session, session in sessions.items()
            if asset_session.asset == asset and session.completed_at is not None
        )
        for index, session_date in enumerate(expected_dates):
            phase_index = index + rule["phase"]
            cycle_position = phase_index % generator["cycleSessions"]
            triangle = (
                cycle_position
                if cycle_position <= generator["halfCycleSessions"]
                else generator["cycleSessions"] - cycle_position
            )
            centered_triangle = triangle - (generator["halfCycleSessions"] // 2)
            regime = regime_basis_points[
                (phase_index // generator["regimeSessions"]) % len(regime_basis_points)
            ]
            oscillation = (((phase_index * 17) % 31) - 15) * 6
            basis_points = 10_000 + centered_triangle * rule["waveBasisPoints"] + regime + oscillation
            close_minor = max(1, rule["baseMinorUnits"] * basis_points // 10_000)
            tick = max(1, rule["baseMinorUnits"] // 10_000)
            open_minor = max(1, close_minor + (((phase_index * 5) % 11) - 5) * tick)
            spread = max(1, rule["baseMinorUnits"] // 2_000 + (phase_index % 7) * tick)
            high_minor = max(open_minor, close_minor) + spread
            low_minor = max(1, min(open_minor, close_minor) - spread)
            volume = rule["volumeBase"] + ((phase_index * 7919) % 100_003)
            result.append(
                DailyBar(
                    asset,
                    session_date,
                    _minor_units(open_minor),
                    _minor_units(high_minor),
                    _minor_units(low_minor),
                    _minor_units(close_minor),
                    Decimal(volume),
                )
            )
    return tuple(result)


def _minor_units(value: int) -> Decimal:
    return Decimal(value).scaleb(-2).quantize(Decimal("0.01"))


def _top_level_provenance(assembly: FixtureAssembly):
    from refinvest.marketdata.domain import CalendarProvenance

    return CalendarProvenance(
        cast(str, assembly.snapshot.calendar_source),
        cast(str, assembly.snapshot.calendar_version),
        cast(str, assembly.snapshot.calendar_hash),
    )


def _validate_definition(definition: Mapping[str, Any]) -> None:
    if definition.get("schemaVersion") != 1 or definition.get("fixtureDefinitionVersion") != 1:
        raise ValueError("Only fixture definition version 1 is supported.")
    if definition.get("fixtureId") != "performance-mvp-daily-v1":
        raise ValueError("Fixture definition has an unexpected immutable fixture ID.")
    if definition.get("frequency") != "DAILY_BAR":
        raise ValueError("Performance fixture must use Daily Bars.")
    asset_rules = definition.get("generator", {}).get("assets", [])
    if [rule.get("symbol") for rule in asset_rules] != list(MVP_ASSETS):
        raise ValueError("Synthetic generator must define the ordered MVP five-asset universe.")


def _git_revision(repository: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()
