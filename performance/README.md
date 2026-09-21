# Deterministic performance fixture

This directory defines the fixed input and reviewed correctness identity for Compute benchmarks.
Compute owns the benchmark runner; integration owns the execution protocol, expected results,
orchestration, and local aggregate reports. Absolute timing reports remain local artifacts.

## Scope and safety

`performance-mvp-daily-v1` is synthetic, non-production data. It is generated without a vendor
API, vendor credential, production database, downloaded market history, randomness, or the wall
clock. It is useful only for repeatable code-path and performance comparisons. It does not claim
to reproduce a real market or support any investment-performance conclusion.

The fixture is separate from `integration-e2e-v1`: the short fixture remains optimized for the
cross-service smoke, while this fixture covers 2015-01-01 through 2024-12-31 and is intentionally
large enough for later benchmark work. Neither publisher changes or deletes the other snapshot.

## Versioned inputs

- `fixture-definition.json` fixes fixture ID/version, creation time, coverage, adjustment policy,
  calendars, assets, and the integer-only synthetic data rules.
- `fixture-manifest.json` fixes the definition hash, calendar provenance, expected session counts,
  artifact byte sizes and SHA-256 values, the storage-independent Compute manifest hash, the
  Compute revision, and its `uv.lock` fingerprint used for generation.
- `scenarios/*.json` wrap the existing `POST /backtests` request payload. They add only benchmark
  metadata; they do not define a second strategy DSL.

The generator uses Compute's installed domain types, XNYS and CRYPTO_UTC calendar generators,
composite provenance, canonical artifact publisher, and artifact loader. Integration does not
construct `manifest.json`, `calendar.json`, or `daily-bars.ndjson` itself. The publisher accepts the
Compute checkout as a parameter and runs in its virtual environment; Compute is not a Git
submodule or integration runtime dependency.

## Synthetic Daily Bar rule

For each asset's ordered expected sessions, version 1 combines a fixed per-asset base and phase
with a 240-session triangular cycle, a 480-session regime sequence, and a small modular
oscillation. All price calculations use integers in minor units and are converted to two-place
`Decimal` values only at the boundary. Open offsets, high/low spreads, and volumes use separate
fixed modular sequences. This produces repeated rising and falling regions while guaranteeing
positive volume and `low <= open/close <= high`. Asset/session output order is canonicalized by
Compute's publisher.

US equities use Compute's locked `exchange-calendars` XNYS generator, including its weekends,
holidays, and close timestamps. BTCUSDT uses Compute's fixed every-UTC-date calendar. The
composite and component source/version/content hashes are pinned in `fixture-manifest.json`; no
holiday list is maintained here. Reproduction requires the recorded dependency lock fingerprint.

## Generate and verify locally

The normal publication command initializes the existing Compute schema flow, publishes only to
the isolated `refinvest_integration` database, writes artifacts below the integration workspace,
and verifies them afterward:

```powershell
.\scripts\publish-performance-fixture.ps1 `
  -EnvironmentFile .\.env.local `
  -ComputeApiPath ..\compute-api\refinvest
```

Optional `-Schema` and `-SnapshotStorageRoot` parameters support isolated schemas and a different
workspace-local artifact root. The default local artifact directory is
`.integration-data/performance-snapshots/performance-mvp-daily-v1`. A second run validates the
existing metadata, storage path, Compute loader result, and expected hashes without overwriting or
republishing it. A matching artifact directory without metadata is validated before its immutable
metadata is inserted. Any mismatch fails closed.

To verify an already generated directory without publication:

```powershell
$python = ..\compute-api\refinvest\.venv\Scripts\python.exe
& $python .\scripts\verify-performance-fixture.py `
  --definition .\performance\fixture-definition.json `
  --fixture-manifest .\performance\fixture-manifest.json `
  --scenario-directory .\performance\scenarios `
  --snapshot-path .\.integration-data\performance-snapshots\performance-mvp-daily-v1 `
  --compute-api-path ..\compute-api\refinvest
```

The verifier checks fixed identity/time/range, all five assets, per-asset session coverage, the
weekend crypto/equity difference, OHLCV validity, calendar provenance, integrity flags, every
artifact hash, Compute loader replay, unique scenario IDs, and Compute transport/domain validation.
The generation path imports no vendor adapter or network client. Publication uses only the local
PostgreSQL connection supplied by the integration environment.

## Scenarios

- `simple-1y`: one QQQ lookback condition over one year and the short same-market path.
- `simple-10y`: the same strategy shape over nearly the full fixture period for period-scaling
  cost.
- `cross-calendar-10y`: BTCUSDT primary sessions and TQQQ execution with lag/holding, including
  weekend signals resolved to a later US-equity session.
- `relative-multi-condition-10y`: two AND-combined MetricReference-to-MetricReference conditions
  with both crypto and US-equity operands.
- `dense-signals`: a condition guaranteed true by positive prices and a two-session holding period,
  designed to exercise duplicate-entry ignore, entry/exit, and trade realization frequently.
- `zero-trades`: a condition guaranteed false by positive prices. Its expected terminal status is
  `COMPLETED` and expected sample warning is `ZERO`.

All scenarios target both future cold- and warm-cache series. Cache preparation and measurement
belong to the benchmark runner, not this fixture stage.

Trade-producing scenarios leave fixed leading lookback and trailing lag/holding buffers inside the
fixture coverage. This prevents a valid strategy from requesting sessions outside the immutable
artifact solely because its benchmark period touches a fixture boundary.

## Git and version lifecycle

Commit the definition, expected manifest, scenarios, scripts, and this documentation. Do not
commit `.integration-data/`, temporary artifact directories, environment files, database URLs,
credentials, or benchmark reports.

Canonicalization version 1 is implemented by the Compute runner. `expected-results.json` records
only values reviewed after two independent discovery invocations agreed on terminal status, trade
count, sample-size warning, and canonical result hash. It is never generated or updated
automatically. Dummy hashes and guessed trade counts are forbidden.

Create a new fixture ID and increment the definition version when any value-generating rule,
calendar range/source/version, asset coverage, adjustment or integrity policy, Compute artifact
contract, or dependency update changes bytes or meaning. Never overwrite artifacts or manifest
expectations for a published fixture ID. A metadata-only documentation correction that cannot
affect generated bytes does not require a new fixture version.

## Expected result review

Before changing `expected-results.json`, run every scenario without `--expected-results` in at
least two separate runner invocations. Confirm that the fixture identity and canonicalization
version are pinned, all runs are `COMPLETED`, all correctness values agree, and the relevant
determinism/cross-calendar regression tests pass. Confirm periods and assets stay inside fixture
coverage. `dense-signals` must produce trades; `zero-trades` must have trade count zero and warning
`ZERO`. Review relative condition ordering and cross-market calendar semantics against existing
regression evidence.

If a reviewed hash later changes, do not hide it in a performance change. Check the calculation
contract/ADR, determinism and cross-calendar regressions, fixture version, and canonicalization
version, and retain review evidence. Prefer a new version when fixture or canonical meaning
changes. Golden acceptance is deliberately a human review step.

## Baseline protocol and commands

`baseline-protocol.json` is the versioned official protocol: warm process cache, three warm-ups,
ten measured iterations in each of two independent runner invocations, and nearest-rank p95.
Compute currently does not implement process-cold, so warm output is never relabeled as cold.
Fixture preparation and verification are outside measured durations. Execution order is scenario
ID then batch number.

Use a smoke run to check wiring and the expected-result gate:

```powershell
.\scripts\run-performance-baseline.ps1 -Mode Smoke `
  -EnvironmentFile .\.env.local `
  -ComputeApiPath ..\compute-api\refinvest `
  -SnapshotStorageRoot .integration-data\performance-fixture-v1-verify
```

Use the fixed defaults for an official baseline:

```powershell
.\scripts\run-performance-baseline.ps1 -Mode Official `
  -EnvironmentFile .\.env.local `
  -ComputeApiPath ..\compute-api\refinvest `
  -SnapshotStorageRoot .integration-data\performance-fixture-v1-verify
```

`Warmups`, `Iterations`, `Batches`, `CacheMode`, `OutputRoot`, sibling paths, `EnvironmentFile`,
`Schema`, and `SnapshotStorageRoot` are explicit parameters. Overrides are recorded in the report;
Official mode rejects values that differ from the protocol. Smoke defaults to zero warm-ups, one
iteration, and one batch and always reports `official=false`.

Each invocation creates a new directory under `.integration-data/performance/baselines/`, with raw
runner reports, `environment.json`, `revisions.json`, `baseline.json`, and `baseline.md`. JSON
preserves nanoseconds, raw successful and failed samples, batch and aggregate summaries,
correctness, and environment identity. Markdown converts durations only for display. The verifier
recomputes statistics and rejects protocol, identity, completeness, correctness, stage, local-path,
or sensitive-data violations.

The directly measured inputs are the Compute revision/runtime, immutable fixture, scenario, and
protocol. Integration identifies orchestration. Context, Core, and Web revisions are compatibility
metadata; Core and Web are not on the direct Compute benchmark path.

An official baseline is neither a production SLO nor evidence from user traffic. No performance
threshold is established here. Absolute values from shared runners or different machines are not
directly comparable; candidate comparisons should use the same machine/runtime and exact protocol.
