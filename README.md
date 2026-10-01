# RefInvest local integration workspace

This directory composes and verifies existing RefInvest services. It does not
own product contracts, domain rules, or service business code.

## Ownership

- `../context` owns ADRs, domain documentation, and OpenAPI specifications.
- `../api/refinvest` owns Core API behavior and its database tables.
- `../compute-api/refinvest` owns Compute behavior, DatasetSnapshots, and its
  database tables.
- `../web/web` owns browser behavior and only calls Core API.
- This workspace owns local process composition, non-production fixtures, and
  cross-service smoke tests.

`context` is the normative source. `compatibility.local.yaml` only records a
specific implementation combination that was tested against it.

## Performance fixture

The deterministic ten-year, five-asset synthetic fixture, six benchmark scenarios, expected-result
review, and baseline protocol are documented in [`performance/README.md`](performance/README.md).
They are separate from the short API-smoke fixture. Check wiring with
`.\scripts\run-performance-baseline.ps1 -Mode Smoke`; use `-Mode Official` only for the fixed,
long-running protocol. Absolute timing reports remain under `.integration-data/`.

## Local observability (optional)

Use host-native Prometheus and Grafana on Windows. This keeps the Compute API and
Worker metrics listeners on their required loopback addresses; a Docker container's
`localhost` cannot scrape the host's `127.0.0.1`. Install Prometheus and Grafana
locally, then pass their executable paths to the launch script. The existing
PostgreSQL Compose service is unchanged. Prometheus and Grafana run only when
requested and bind to `127.0.0.1:9090` and `127.0.0.1:3002`. Prometheus scrapes
every 5 seconds and keeps two days of data under ignored `.integration-data/`.

```powershell
# After the normal database/fixture setup, in separate local terminals:
# Start Prometheus first; when the env value is absent the harness creates an ignored per-run scrape secret.
$prometheus = 'C:\tools\prometheus\prometheus.exe'
.\scripts\start-observability.ps1 -PrometheusExe $prometheus -PrometheusOnly
.\scripts\start-compute-api.ps1 -Observability
.\scripts\start-compute-worker.ps1 -Observability
.\scripts\start-core-api.ps1 -Observability

# Supply locally installed binaries and a private password; never commit the password.
$password = Read-Host 'Local Grafana admin password' -AsSecureString
.\scripts\start-observability.ps1 -PrometheusExe 'C:\tools\prometheus\prometheus.exe' `
  -GrafanaExe 'C:\tools\grafana\bin\grafana.exe' -GrafanaAdminPassword $password
Remove-Variable password

# Inspect http://127.0.0.1:9090/targets and open http://127.0.0.1:3002.
# Grafana's provisioned "RefInvest local flow and timing" dashboard is in RefInvest local.
.\scripts\stop-observability.ps1
.\scripts\stop-integration.ps1
```

The three scrape jobs are `core-api`, `compute-api`, and `compute-worker`.
Compute API and Worker metrics are separate process-local registries on
`127.0.0.1:9101` and `127.0.0.1:9102`. The launch switches enable metrics only
for those local processes. Do not expose these ports, Prometheus, or Grafana on
another interface. The dashboard uses 5-minute windows; allow at least two
scrapes and run the normal API smoke to produce observations. A blank series
does not establish that collection works.

The `core-api` job uses a dedicated local scrape account and a password file
generated under ignored `.integration-data/observability/`. Core accepts it
only on the loopback metrics endpoint while `dev-observability` is active.
Unauthenticated requests still receive 401. Compute does not currently
expose production stage, cache, or active-job gauge metrics, so the dashboard
does not claim to show them. Worker attempt duration includes process/IPC and
heartbeat shutdown, not just engine execution.

## Local steady/burst load exploration

After the Quick start database/fixture setup, start Core, Compute API, and
Compute Worker with the local scripts above. Keep their existing
`refinvest_integration` database and immutable `integration-e2e-v1` fixture;
install local `psql` for a read-only DB status check. The command verifies
OpenAPI sync, creates a separate synthetic PRO member and StrategyVersion,
performs one warm-up run, then executes three same-condition processing repetitions and
three separate saturation/rejection repetitions with a drain and cooldown between them. It does not start or stop services, alter the
fixture, or delete existing rows.

```powershell
.\scripts\run-load-test.ps1
# Short check with one processing repetition and one attempted saturation repetition.
.\scripts\run-load-test.ps1 -SteadyRate 1 -SteadyDuration 1 -BurstRate 10 -BurstDuration 1 `
  -MaxSubmissions 10 -Concurrency 8 -Repetitions 1
```

Defaults are processing 0.2 submissions/s for 30 seconds and saturation 2/s for 10
seconds, repeated three times, at most 30 submissions per repetition and 3 concurrent submission HTTP
requests. Terminal polling is limited to 120 seconds per accepted run at a
2-second interval; cooldown is 5 seconds. These bounds can be adjusted with
`-SteadyRate`, `-BurstRate`, `-SteadyDuration`, `-BurstDuration`,
`-MaxSubmissions`, `-Concurrency`, `-TerminalTimeout`, `-PollInterval`,
`-Cooldown`, and `-Repetitions`. The Python runner enforces upper limits. Polling to learn terminal
status is recorded separately from submission traffic; it creates no new job.

Each invocation writes `result.json` and `summary.md` under
`.integration-data/performance/load-tests/<timestamp>/`. The report includes
raw submission records, per-repetition request/accept/reject/failure/timeout/completion counts,
planned and actual submission rates, accepted-work and rejection-response latency distributions,
queue/worker metrics, fixture hashes, revisions, a read-only DB boundary comparison, clock-skew
estimates, missing observations, and available Prometheus measurements. Rejected requests are never
included in accepted-work terminal latency. The run fails when the configured saturation scenario
produces no rejection because that did not exercise the requested overload boundary.
Terminal latency includes every observed `COMPLETED` and `FAILED` transition;
timeouts and missing terminals are counted as censored observations, never as
successful latency samples. It is measured from submission start until the
client first sees terminal state, so it includes up to one poll interval of
observation delay. A nonzero exit means the run failed; inspect its
report, integration logs, and DB status. Core Prometheus values are `N/A` if
its target is DOWN; `observability_status` then remains `partial`.
This is runtime bottleneck exploration, not
the fixed-input benchmark or a performance gate. Stop only services you started
using `stop-integration.ps1 -Service <name>`; reports and submitted integration
rows remain for review.

The stage model distinguishes client submission, Core durable enqueue, Compute durable acceptance,
Worker queue wait, Worker attempt, Compute terminal commit, Core poll observation, Core terminal commit,
and client terminal observation. The report identifies the load generator, Core HTTP client, and Worker attempt
monotonic timers. Core scheduler lag and Compute queue wait use wall clocks, so they remain estimates rather than
exact cross-process durations.
Raw database and HTTP wall-clock timestamps only prove that boundaries occurred: the report estimates and
displays clock skew and never subtracts Core and Compute timestamps as an exact stage duration. If repeated
evidence cannot separate a candidate, the report remains `indeterminate` and names the missing per-run
instrumentation. A Compute execution candidate requires a new same-environment `-Mode Official` benchmark
before optimization; an `official=false` smoke artifact is never relabeled. A Core poll/persistence candidate
instead requires the same E2E load conditions before and after, because a Compute-only benchmark cannot
establish that effect.

## Trading Review reconstruction fixtures

Binance USDⓈ-M Linear Perpetual One-way reconstruction의 합성 Golden Dataset과 fixture catalog는
[`fixtures/trading-review/binance-usds-v1`](fixtures/trading-review/binance-usds-v1/README.md)에 있다.
정적 schema, provenance, 보존식, checksum, 개인정보 namespace와 손상 거절 검증은 다음 명령으로 실행한다.

```powershell
.\scripts\verify-trading-review-fixtures.ps1
```

이 검증은 reconstruction을 구현하거나 실행하지 않으며, Trading Review API가 확정되기 전까지 endpoint 기반
E2E를 추측하지 않는다. Cross-service compatibility/E2E 전에는 기존 규칙대로 먼저
`.\scripts\verify-contract-sync.ps1`을 실행한다.

## Quick start: API integration smoke

Copy `.env.example` to `.env.local`. Use an isolated database named
`refinvest_integration`; it must not point at a normal development or
production database. A locally running PostgreSQL instance is supported
directly; Docker Compose is optional.

```powershell
# Optional Docker PostgreSQL
docker compose --env-file .env.local up -d postgres
docker compose --env-file .env.local ps

# Required before every cross-service test.
.\scripts\verify-contract-sync.ps1

# Compute-owned schema and deterministic fixture.
.\scripts\initialize-compute-schema.ps1
.\scripts\publish-compute-fixture.ps1
```

The schema script uses local `psql` when available and falls back to Docker
Compose otherwise. It records only Compute migration filenames in the
integration-owned `integration_schema_migrations` table. Run it against a new
integration database. If a database predates this harness and has Compute
tables without those records, do not let the script infer migration history:
recreate the isolated database or explicitly inspect and record its history
before using it.

Start the backend processes in this order:

```powershell
.\scripts\start-compute-api.ps1
.\scripts\start-compute-worker.ps1
.\scripts\start-core-api.ps1
```

The start scripts record only the processes they launch in ignored,
profile-scoped `.integration-data/processes/<registry-id>.json` registries.
The default stack uses `default`; API smoke, browser acceptance, observability,
and each reliability run use separate registry IDs. A record contains PID and
start time (OS process identity) plus a secret-free configuration identity:
service role, execution profile, checkout path/revision, listen address,
database name/schema, snapshot profile/root, environment-file path, and the
service endpoints that affect topology. Dirty checkout state is diagnostic and
is not part of the reuse fingerprint.

A start script reuses a live recorded process only when PID/start time, role,
profile, repository revision, configuration fingerprint, listener ownership,
and health all match. A healthy endpoint is not sufficient. `MISMATCH`,
`LEGACY`, `STALE`, or `UNOWNED_PORT` is a hard failure and no process is stopped
or restarted automatically. Use the lifecycle helpers to inspect or explicitly
stop only the recorded profile:

```powershell
.\scripts\status-integration.ps1
.\scripts\stop-integration.ps1
# Or stop only a recorded service.
.\scripts\stop-integration.ps1 -Service core-api
# Example for an acceptance profile.
.\scripts\status-integration.ps1 -Profile web-acceptance -RegistryId web-acceptance
.\scripts\stop-integration.ps1 -Profile web-acceptance -RegistryId web-acceptance
```

`stop-integration.ps1` verifies the recorded launcher PID and start time
before stopping its process tree. It never stops a listener or worker that was
not recorded by an integration start script. If status reports a schema
mismatch such as `compute-api=ri_rel_*` versus `compute-worker=public`, stop the
named service with the exact profile/registry command printed by status and
then start the intended stack. Do not kill the port owner directly.

`status-integration.ps1` reports PID validation, listener ownership, health,
recorded/current revisions, database, schema, and identity state. `MATCH` is
reusable; `MISMATCH` is a live but incompatible managed service; `LEGACY` lacks
configuration identity; `STALE` no longer identifies the recorded OS process;
and `UNOWNED_PORT` means integration has no matching ownership record. Legacy
`.integration-data/processes.json` records are never assumed compatible; after
inspection, they can be stopped explicitly with
`.\scripts\stop-integration.ps1 -Legacy`.

Before submitting jobs, API smoke, Web acceptance, controlled load, and the
reliability recovery path run a cross-service topology preflight. It requires
Core/Compute to use the same integration database, Compute API/Worker to use
the same schema and snapshot identity, Core to target the recorded Compute API
(or its recorded fault proxy), Web to target the recorded Core endpoint, and
Prometheus targets to match recorded observability endpoints.

`start-core-api.ps1` also sets Gradle's user home to the ignored
`.integration-data/gradle` directory. This avoids a global Gradle cache and
keeps wrapper downloads and lock files writable in restricted local shells.

Wait for both health endpoints, then seed the test Member and execute the API
smoke flow.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8080/actuator/health

$token = .\scripts\seed-core-member.ps1 -Tier PRO
.\scripts\run-api-smoke.ps1 -AccessToken ($token | Select-Object -Last 1)

# Run this after the default API smoke: it publishes two additional immutable
# provenance-test snapshots to the same isolated database.
.\scripts\verify-binance-public-data-fixture.ps1
```

The smoke uses normal HS256 Access JWT verification plus a CSRF cookie/header
pair. It requires Strategy creation, StrategyVersion creation, Compute
dispatch, and `COMPLETED` results with `datasetSnapshotId=integration-e2e-v1`.
It first preserves the trade-free same-market empty-state path, then runs a
deterministic Cross-Market scenario (`BTCUSDT` signal → `TQQQ` execution). The
second path requires realized trades, strategy and Buy & Hold equity curves,
signal/entry/exit timestamps, and the `CROSS_MARKET` relation. It asserts the
distinct BTCUSDT and TQQQ benchmark date sequences directly, so a Core or
Compute regression that forward-fills across their different calendars fails
the smoke. A third Cross-Market scenario uses a fixed 2% entry/exit commission
against the same immutable price fixture; it verifies a negative strategy MDD
and an equity point below its running peak, providing deterministic evidence
for Drawdown visualization without changing production or fixture data. It then
replays the identical request and requires the requested/actual period, fee
model, snapshot, engine version, and every result field except the per-run ID
to be identical. It also calls the version-specific latest-completed endpoint
before execution, after the initial run, and after the replay. This verifies
the no-result `run: null` response and that the replay becomes the displayed
latest completed result without relying on a paginated strategy-level history.
It additionally uses a separate integration member with a valid JWT to verify
that a foreign-owned StrategyVersion returns the contract's `404` response.
Finally, it starts a fixture-range-missing request and requires a terminal
`FAILED` result with `CALENDAR_RESOLUTION_FAILED`; the latest-completed endpoint
must still return the earlier successful run, while latest-terminal returns the
failed run. This prevents a refresh from hiding either the last usable chart or
the most recent failure.

`verify-binance-public-data-fixture.ps1` is separate from the cross-service
smoke so it can exercise the real Compute PostgreSQL archive-revision and
DatasetSnapshot storage path without calling an external vendor. It serves two
fixed, checksum-valid monthly BTCUSDT ZIP fixtures for the same Binance URI:
one original and one revised payload. It confirms that the verified URI,
SHA-256, and retrieval timestamp are stored in each snapshot's provenance,
that a matching hash is reusable, and that a changed hash creates a second
immutable revision without changing the first snapshot. It uses the separate
`integration_binance_fixture` schema in the isolated integration database, so
its provenance-test snapshots cannot become the default snapshot selected by
the Core-to-Compute API smoke. Its artifacts are likewise isolated under the
ignored `.integration-data/binance-fixture-snapshots` directory.

## Durable dispatch and idempotency reliability

The reliability harness uses the real Compute and Core HTTP endpoints, isolated
per-run PostgreSQL schemas inside `refinvest_integration`, and integration-owned
processes. It also starts a local fault proxy that forwards the first Compute
submission, observes its `202`, and closes the Core connection before returning
the response.

```powershell
.\scripts\run-durable-dispatch-reliability.ps1 `
  -Concurrency 20 -Rounds 20 -DispatchRuns 10
```

The command verifies contract sync before and after the run, executes the
Compute PostgreSQL integration tests, performs a fresh insert race in every
concurrency round, checks payload conflicts, submits Core runs while Compute is
down, then starts Compute and waits for terminal recovery. Machine-readable
results and partial failure artifacts are written under the ignored
`.integration-data/reports/durable-dispatch-*` directory. The command stops
only processes registered by this harness. See
`docs/DURABLE_DISPATCH_RELIABILITY.md` for assertions, safety boundaries, and
report semantics.

## Web and authentication acceptance

Use the same `localhost` host for Web and Core. With the integration default
`SameSite=Lax`, `localhost` and `127.0.0.1` are different sites, so a Web app
on `localhost` cannot reliably send Core cookies at `127.0.0.1`.

```powershell
# Starts Web with NEXT_PUBLIC_API_BASE_URL=http://localhost:8080 by default.
.\scripts\start-web.ps1
```

### Automated result-restoration browser acceptance

The Web repository owns the Playwright test. The integration harness only
starts the real local stack, seeds an isolated PRO member, and supplies its
short-lived integration token to the browser process without logging it. The
test creates its own `BTCUSDT` signal → `TQQQ` strategy version and a completed
run using the immutable `integration-e2e-v1` fixture with `commission: 0.02`.
It opens the version URL directly, verifies the visible Equity Curve, Drawdown,
Strategy/Buy & Hold comparison, timeline, and negative MDD, then verifies the
same result after reload. It then creates a deterministic calendar-resolution
failure for the same version and verifies direct entry and reload retain the
earlier successful charts while showing a user-safe failure message and its
structured failure code.

After completing the Quick start setup, install the Web test dependency and
Chromium once, then run the combined acceptance command:

```powershell
pnpm --dir ..\web\web install
pnpm --dir ..\web\web exec playwright install chromium
.\scripts\run-web-acceptance-ci.ps1
```

This command first executes `verify-contract-sync` and the Core→Compute API
smoke. It uses response polling and visible-page conditions instead of fixed
browser sleeps. Run `stop-integration.ps1` afterwards when using the local
harness.

`run-auth-smoke.ps1` checks actual Core HTTP/Cookie behavior after a real OAuth
login has issued a refresh cookie. Supply a fresh token for each scenario; a
refresh-replay check intentionally revokes that token family. The script never
prints token values.

```powershell
.\scripts\run-auth-smoke.ps1 -Scenario RefreshRotation -RefreshToken '<fresh token>'
.\scripts\run-auth-smoke.ps1 -Scenario Logout -RefreshToken '<different fresh token>'
```

For manual provider acceptance, set `REFINVEST_OAUTH_ENABLED=true` and all six
provider credentials in `.env.local`, then start Core. Register each provider
callback as `http://localhost:8080/login/oauth2/code/{provider}`. Verify that
Kakao, Naver, and Google each return a provider authorization `302`, that a
normal `returnTo` (for example `/strategies?tab=active`) survives the callback,
and that external URLs, `//host/path`, and backslash paths are rejected or
normalized to `/`. Do not add fake providers, callback routes, or credentials
to automate this step.

After credentials are configured, the authorization-start portion is repeatable:

```powershell
.\scripts\run-oauth-start-smoke.ps1
```

It checks each provider authorization redirect and that the redirect includes
OAuth state. It also expects unsafe `returnTo` values to receive the contract's
`400` response. Callback completion remains a browser/manual check because it
requires a real provider account and registered callback.

For each configured provider, use this browser acceptance checklist without
copying Cookie or token values from browser tools:

1. Open `http://localhost:3001/login?returnTo=%2Fstrategies%3Ftab%3Dactive`
   and complete the provider login.
2. Confirm the callback returns to `/strategies?tab=active`.
3. Use **Account** -> **Sign out** and confirm the Web app moves to `/login`.
4. Reopen `http://localhost:3001/strategies?tab=active`; it must move to
   `/login?returnTo=%2Fstrategies%3Ftab%3Dactive` after the protected request
   cannot refresh the session.

The final step verifies the actual browser path: Core clears the OAuth HTTP
session as well as its Access/Refresh cookies and Refresh Token family; Web
then handles the protected-request `401`, failed refresh `401`, and preserved
`returnTo`. A refresh `403` caused by a missing or invalid CSRF header remains
a security error and must not be treated as a successful refresh or retried.

## Service topology

```text
Browser/Web (localhost:3001)
        |
        v
Core API (localhost:8080) ---- internal API key ----> Compute (localhost:8000)
        |                                                   |
        +---------------- shared PostgreSQL ----------------+
```

Core's `COMPUTE_API_KEY` is derived from the single
`REFINVEST_INTERNAL_API_KEY` environment value. Do not add a second key with a
different value. Compute receives that same value as its internal API key.

## Compatibility evidence

After a successful run, copy `compatibility.example.yaml` to
`compatibility.local.yaml` and record the exact Core, Compute, Web, and context
revisions tested together. This is evidence only; it never supersedes context.

## GitHub Actions contract sync

`.github/workflows/contract-sync.yml` runs the mandatory OpenAPI snapshot check
for integration pull requests, pushes to `main`, and manual dispatches. It
checks out `context`, `core-api`, and `compute-api` beside this repository using
the same sibling paths as the local harness.

Before enabling the workflow for private sibling repositories, install a GitHub
App on exactly `integration`, `context`, `core-api`, and `compute-api` with
`Contents: Read` permission. Configure the integration repository with:

- Actions variable `REFINVEST_CI_APP_CLIENT_ID`
- Actions secret `REFINVEST_CI_APP_PRIVATE_KEY`

The workflow creates a short-lived token scoped to the three sibling
repositories. It does not receive OAuth credentials, database passwords, or
internal API keys. Pull requests from forks are skipped because repository
secrets are intentionally unavailable to them.

## GitHub Actions API smoke

`.github/workflows/api-smoke.yml` uses an ephemeral PostgreSQL service and the
same deterministic Compute fixture as the local harness. It installs no OAuth
credentials and runs the actual Core-to-Compute path: strategy creation,
dispatch, worker execution, and terminal-result persistence. The workflow
first repeats the mandatory OpenAPI snapshot check, then uploads Core, Compute,
and worker logs as `api-smoke-logs` on both success and failure.

The workflow uses the same read-only GitHub App configuration as contract
sync. It is intentionally skipped for fork pull requests because the App
private key is unavailable there.

## Next scope

Web browser acceptance now runs alongside the API smoke. Web continues to call
Core only, never Compute directly.
