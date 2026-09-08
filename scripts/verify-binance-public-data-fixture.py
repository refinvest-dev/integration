"""Verify deterministic Binance Public Data provenance through the Compute storage path.

This is an integration-owned fixture: no request reaches data.binance.vision and no
vendor credential is used.  The two ZIP payloads are fixed copies of a one-row
monthly BTCUSDT archive.  They deliberately share an archive URI but have different
verified SHA-256 values, which models a corrected upstream archive revision.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from refinvest.app.config import Settings, get_settings
from refinvest.app.snapshot_storage import (
    create_snapshot_artifact_publisher,
    create_snapshot_execution_data_loader,
    snapshot_storage_path,
)
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.calendars.crypto_utc import build_crypto_utc_calendar
from refinvest.ingestion.calendars.us_equity import UsEquityCalendarArtifact
from refinvest.ingestion.snapshot.mvp import (
    MvpSnapshotIngestionCoordinator,
    MvpSnapshotRequest,
)
from refinvest.ingestion.vendors.binance_btcusdt import (
    BinanceBtcusdtDailyBarsClient,
    BtcusdtArchiveBatch,
)
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)
from refinvest.persistence.postgres.binance_archive_revisions import (
    PostgresBinanceArchiveRevisionRepository,
)
from refinvest.persistence.postgres.dataset_snapshots import (
    PostgresDatasetSnapshotRepository,
)

_FIXTURE_DATE = date(2025, 1, 2)
_RETRIEVED_AT = datetime(2025, 2, 1, tzinfo=UTC)
_ARCHIVE_URI = (
    "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/"
    "BTCUSDT-1d-2025-01.zip"
)
_ARCHIVE_NAME = "BTCUSDT-1d-2025-01.zip"
_FIRST_SNAPSHOT_ID = "integration-binance-public-data-v1"
_SECOND_SNAPSHOT_ID = "integration-binance-public-data-v2"

# ZIP_STORED archives with a fixed member timestamp.  Keeping encoded bytes in this
# harness makes both the ZIP and the companion CHECKSUM response reproducible.
_ORIGINAL_ARCHIVE = base64.b64decode(
    "UEsDBBQAAAAAAAAAIVpsvG8YUAAAAFAAAAAWAAAAQlRDVVNEVC0xZC0yMDI1LTAxLmNzdm9wZW5fdGltZSxvcGVuLGhpZ2gsbG93LGNsb3NlLHZvbHVtZQoxNzM1Nzc2MDAwMDAwLDkwMDAwLDkxMDAwLDg5MDAwLDkwNTAwLDEyLjUKUEsBAhQAFAAAAAAAAAAhWmy8bxhQAAAAUAAAABYAAAAAAAAAAAAAAIABAAAAAEJUQ1VTRFQtMWQtMjAyNS0wMS5jc3ZQSwUGAAAAAAEAAQBEAAAAhAAAAAAA"
)
_REVISED_ARCHIVE = base64.b64decode(
    "UEsDBBQAAAAAAAAAIVoSetXOUAAAAFAAAAAWAAAAQlRDVVNEVC0xZC0yMDI1LTAxLmNzdm9wZW5fdGltZSxvcGVuLGhpZ2gsbG93LGNsb3NlLHZvbHVtZQoxNzM1Nzc2MDAwMDAwLDkwMDAwLDkyMDAwLDg5MDAwLDkxNTAwLDEyLjUKUEsBAhQAFAAAAAAAAAAhWhJ61c5QAAAAUAAAABYAAAAAAAAAAAAAAIABAAAAAEJUQ1VTRFQtMWQtMjAyNS0wMS5jc3ZQSwUGAAAAAAEAAQBEAAAAhAAAAAAA"
)
_ORIGINAL_SHA256 = "6a27f51dd84075be976751363e7b3917a6d830763f08418be716faec8366fc5e"
_REVISED_SHA256 = "9556033a9725204a1a3811c377a0cfc903332d86ca2fedaeeecb4189ef52cb41"
_ETF_ASSETS = ("QQQ", "SPY", "TQQQ", "SOXL")


def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("REFINVEST_DATABASE_URL is required.")

    metadata = PostgresDatasetSnapshotRepository(settings.database_url)
    revisions = PostgresBinanceArchiveRevisionRepository(settings.database_url)
    original = _fetch(_ORIGINAL_ARCHIVE, revisions)
    original_again = _fetch(_ORIGINAL_ARCHIVE, revisions)
    revised = _fetch(_REVISED_ARCHIVE, revisions)

    _assert_batch(original, _ORIGINAL_SHA256, Decimal(90500))
    _assert_batch(original_again, _ORIGINAL_SHA256, Decimal(90500))
    _assert_batch(revised, _REVISED_SHA256, Decimal(91500))
    if original.source_artifacts == revised.source_artifacts:
        raise AssertionError("A changed archive hash must create a distinct provenance revision.")
    if revisions.get(_ARCHIVE_URI, _ORIGINAL_SHA256) is None:
        raise AssertionError("Original verified archive revision was not stored.")
    if revisions.get(_ARCHIVE_URI, _REVISED_SHA256) is None:
        raise AssertionError("Revised verified archive revision was not stored.")

    first = _publish_if_absent(
        settings, metadata, revisions, _FIRST_SNAPSHOT_ID, _ORIGINAL_ARCHIVE
    )
    second = _publish_if_absent(
        settings, metadata, revisions, _SECOND_SNAPSHOT_ID, _REVISED_ARCHIVE
    )
    _assert_snapshot(first, _ORIGINAL_SHA256, Decimal(90500), settings)
    _assert_snapshot(second, _REVISED_SHA256, Decimal(91500), settings)

    print(
        json.dumps(
            {
                "archiveUri": _ARCHIVE_URI,
                "originalSha256": _ORIGINAL_SHA256,
                "revisedSha256": _REVISED_SHA256,
                "snapshots": [first.snapshot_id, second.snapshot_id],
            },
            sort_keys=True,
        )
    )


def _fetch(
    archive: bytes, revisions: PostgresBinanceArchiveRevisionRepository
) -> BtcusdtArchiveBatch:
    checksum = f"{hashlib.sha256(archive).hexdigest()}  {_ARCHIVE_NAME}\n".encode()

    def download(uri: str) -> bytes:
        if uri == _ARCHIVE_URI:
            return archive
        if uri == f"{_ARCHIVE_URI}.CHECKSUM":
            return checksum
        raise AssertionError(f"Unexpected external download: {uri}")

    return BinanceBtcusdtDailyBarsClient(
        download,
        now=lambda: _RETRIEVED_AT,
        revisions=revisions,
    ).fetch_daily_bars(_FIXTURE_DATE, _FIXTURE_DATE)


def _publish_if_absent(
    settings: Settings,
    metadata: PostgresDatasetSnapshotRepository,
    revisions: PostgresBinanceArchiveRevisionRepository,
    snapshot_id: str,
    archive: bytes,
) -> DatasetSnapshot:
    existing = metadata.get(snapshot_id)
    if existing is not None:
        return existing

    coordinator = MvpSnapshotIngestionCoordinator(
        _fixture_us_equity_calendar,
        build_crypto_utc_calendar,
        _FixtureEtfBars(),
        _fixture_btcusdt_ingestor(archive, revisions),
        create_snapshot_artifact_publisher(settings),
        metadata,
    )
    coordinator.publish(
        MvpSnapshotRequest(
            snapshot_id=snapshot_id,
            created_at=_RETRIEVED_AT,
            source="BINANCE_PUBLIC_DATA",
            coverage={"firstDate": _FIXTURE_DATE.isoformat(), "lastDate": _FIXTURE_DATE.isoformat()},
            adjustment_policy="split-adjusted-ohlcv-cash-dividends-unadjusted",
            storage_path=snapshot_storage_path(settings, snapshot_id),
            corporate_actions_applied=True,
            point_in_time_validation_passed=True,
        ),
        _FIXTURE_DATE,
        _FIXTURE_DATE,
        {},
    )
    published = metadata.get(snapshot_id)
    if published is None:
        raise AssertionError("Published DatasetSnapshot metadata was not found.")
    return published


def _fixture_btcusdt_ingestor(
    archive: bytes, revisions: PostgresBinanceArchiveRevisionRepository
) -> BinanceBtcusdtDailyBarsClient:
    checksum = f"{hashlib.sha256(archive).hexdigest()}  {_ARCHIVE_NAME}\n".encode()

    def download(uri: str) -> bytes:
        if uri == _ARCHIVE_URI:
            return archive
        if uri == f"{_ARCHIVE_URI}.CHECKSUM":
            return checksum
        raise AssertionError(f"Unexpected external download: {uri}")

    return BinanceBtcusdtDailyBarsClient(
        download,
        now=lambda: _RETRIEVED_AT,
        revisions=revisions,
    )


def _fixture_us_equity_calendar(
    start: date, end: date, _: Mapping[date, datetime | None]
) -> UsEquityCalendarArtifact:
    sessions = {
        AssetSession(asset, session_date): CalendarSession(
            CalendarSessionState.EXPECTED,
            datetime.combine(session_date, time(21), UTC),
        )
        for asset in _ETF_ASSETS
        for session_date in _dates_inclusive(start, end)
    }
    return UsEquityCalendarArtifact(
        CalendarProvenance("integration-fixture", "v1", "binance-public-data-v1"),
        sessions,
    )


class _FixtureEtfBars:
    def fetch_for_calendar(
        self,
        sessions: Mapping[AssetSession, CalendarSession],
        start: date,
        end: date,
    ) -> tuple[DailyBar, ...]:
        return tuple(
            DailyBar(
                asset,
                session_date,
                Decimal(100),
                Decimal(100),
                Decimal(100),
                Decimal(100),
                Decimal(1000),
            )
            for asset in _ETF_ASSETS
            for session_date in _dates_inclusive(start, end)
            if sessions[AssetSession(asset, session_date)].state is CalendarSessionState.EXPECTED
        )


def _assert_batch(batch: BtcusdtArchiveBatch, sha256: str, close: Decimal) -> None:
    artifacts = batch.source_artifacts
    bars = batch.daily_bars
    if len(artifacts) != 1 or artifacts[0].uri != _ARCHIVE_URI or artifacts[0].sha256 != sha256:
        raise AssertionError("Archive provenance did not match the fixed fixture.")
    if len(bars) != 1 or bars[0].close != close:
        raise AssertionError("Archive bar did not match the fixed fixture.")


def _assert_snapshot(
    snapshot: DatasetSnapshot, sha256: str, close: Decimal, settings: Settings
) -> None:
    if snapshot.source != "BINANCE_PUBLIC_DATA":
        raise AssertionError("DatasetSnapshot source was not BINANCE_PUBLIC_DATA.")
    if len(snapshot.source_artifacts) != 1:
        raise AssertionError("DatasetSnapshot did not preserve one archive provenance record.")
    artifact = snapshot.source_artifacts[0]
    if artifact.uri != _ARCHIVE_URI or artifact.sha256 != sha256 or artifact.retrieved_at != _RETRIEVED_AT:
        raise AssertionError("DatasetSnapshot provenance does not match the verified archive revision.")
    execution_data = create_snapshot_execution_data_loader(settings).load(snapshot)
    bar = execution_data.market_data.get_daily_bar("BTCUSDT", _FIXTURE_DATE).bar
    if bar.close != close:
        raise AssertionError("DatasetSnapshot did not preserve its BTCUSDT archive revision.")


def _dates_inclusive(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


if __name__ == "__main__":
    main()
