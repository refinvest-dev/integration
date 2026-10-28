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

## Trading Normalization runtime contract

`scripts/test-trading-normalization-runtime-contract.ps1`는 Core public API의 합성 CSV
업로드와 Validate 명령에서 시작하여 durable dispatch, 실제 Compute HTTP DTO, worker,
terminal payload까지 검증한다. Context OpenAPI와 Core·Compute·Web snapshot, Context
identity fixed vector의 byte equality 및 canonical UTF-8/SHA-256 검사는 프로세스를
시작하기 전에 자동 실행된다. Core LedgerRevision publication은 범위에 포함되지 않는다.

```powershell
.\scripts\verify-contract-sync.ps1 -IncludeWeb
.\scripts\verify-trading-review-fixtures.ps1 -Dataset raw-v3
.\scripts\test-trading-normalization-runtime-contract.ps1
# 기본 포트가 사용 중이면 명시적으로 변경한다.
.\scripts\test-trading-normalization-runtime-contract.ps1 -CorePort 8180 -ComputePort 8181
```

기본 sibling checkout은 `../context`, `../api/refinvest`, `../compute-api/refinvest`,
`../web/web`이다. 다른 위치에서는 `-ContextPath`, `-CorePath`, `-ComputePath`,
`-WebPath`를 지정한다. 로컬 PostgreSQL의 전용 DB `refinvest_integration`과
`.env.local`의 `REFINVEST_INTERNAL_API_KEY`가 필요하다. 스크립트는 DB 이름과
loopback 접속 대상을 확인하고, Core는 `public`, Compute는 실행마다 생성하는
`tnorm_*` schema를 사용한다. 합성 Artifact는 Core의 로컬 암호화 raw storage에
업로드되며 Compute는 Core가 발급한 read grant URL로 받는다. 외부 storage와
Binance 네트워크는 사용하지 않는다.

검증 범위는 정상 identity·hash·VersionSet 왕복, terminal payload OpenAPI,
locator refresh, 입력 hash 불일치, 지원하지 않는 hash 버전, source bytes hash
불일치, idempotency conflict, Compute API 재시작 후 persistence와 replay다.
locator refresh는 worker를 멈춘 사이 이 실행의 Compute schema에서 최초 grant
revision의 만료 시각만 조정하는 test fault를 사용한다. 이후 Core가 실제 새 grant를
발급하고 같은 job이 완료되는지 확인한다. 종료 시 시작한 프로세스를 정리하며
DB와 로그는 `.integration-data/`에 보존한다. 실패하면 non-zero로 종료하고
`evidence.json`, 로그 경로, Core Session·Run·Compute job ID와 안전한 오류 코드를
출력한다. 해당 디렉터리와 `.env.local`은 커밋하지 않는다.

`TR-ACC-IMPORT-QUALITY-001`도 같은 runtime script에서 검증한다. 비민감 synthetic Trade
History에서 필수 `Maker` column만 제거해 Core upload/validation부터 Compute `FAILED`와
Core terminal polling까지 실행한다. 공개 `REJECTED` 응답의 `qualitySummary: null`, safe
failure code, LedgerRevision·ACK 미생성 및 Book latest pointer 불변을 실제 HTTP/DB 경로에서 확인한다.

## Trading LedgerRevision publication E2E

`scripts/test-trading-normalization-publication.ps1`는 위 runtime의 환경 구성과 프로세스
helper를 재사용해 정상 결과의 Core publication까지 검증한다. 로컬 PostgreSQL과 `.env.local`이
필요하다. 기본 sibling checkout 경로는 위와 같고 `-ContextPath`, `-CorePath`,
`-ComputePath`, `-WebPath`, `-EnvironmentFile`, `-CorePort`, `-ComputePort`,
`-TimeoutSeconds`를 지정할 수 있다.

```powershell
.\scripts\verify-contract-sync.ps1 -IncludeWeb
.\scripts\test-trading-normalization-publication.ps1
# 기본 포트가 사용 중인 경우
.\scripts\test-trading-normalization-publication.ps1 -CorePort 8380 -ComputePort 8381
```

비민감 synthetic Binance Trade/Position History 두 파일을 Core public API에 업로드하고
validation, durable dispatch, Compute worker terminal `COMPLETED`, Core Session `ACCEPTED`,
Run·dispatch `PUBLISHED`, LedgerRevision 정확히 하나, Book latest pointer를 확인한다.
Context fixed vector self-test와 실제 terminal bytes SHA-256, 10개 logical count, 9개
section digest, manifest/result hash를 독립 계산한다. 발행된 canonical record, Evidence,
ReviewUnit, allocation, quality/capability/manifest, DB count와 staging cleanup을 검사한다.
Core가 `RESULT_READY`에 도달한 뒤 publication 잠금을 잠시 잡아 commit된 streaming
staging의 identity·section count도 terminal descriptor와 직접 비교한다.
Compute API 중단으로 ACK의 일시 실패를 만들고, Revision 보존과 ACK 재시도·완료를 검증한다.
추가 polling 후 중복 Revision, record, allocation이 없는지도 확인한다.

실행별 안전한 상태·count·hash prefix와 로그 경로는
`.integration-data/trading-normalization/<runId>/evidence.json`에 남긴다. 실패는 non-zero다.
전체 crash recovery 조합과 limited-heap stress 검증은 후속 작업이다. 이 fixture는
고정된 source ID·timestamp를 사용하지만 Core가 HTTP 생성 시 부여하는 Book/Artifact ID가
inputSetHash에 포함되므로 실행 간 전체 resultHash 값은 고정되지 않는다. 매 실행마다
Context 규칙으로 실제 hash를 독립 재계산해 일치 여부를 검증한다.

## Trading Normalization result version pin E2E

```powershell
.\scripts\test-trading-normalization-result-version-pin.ps1 -CorePort 8680 -ComputePort 8681
```

기존 runtime/publication harness와 동일한 경로·환경·DB parameter를 받는다. 첫 실행 검사는
`verify-contract-sync.ps1 -IncludeWeb`이며 DB는 loopback의 `refinvest_integration`만 허용한다.
Core 공개 합성 Binance 업로드·validation부터 실제 durable create, accepted/status,
terminal descriptor/payload, LedgerRevision 및 ACK까지 V1/V1 pin을 대조한다.
기존 V1 fixed vector, 9개 section digest, 단일 publication과 ACK 실패·재시도를 재사용한다.
loopback Integration 프록시가 ACK 요청 시 별도 DB connection에서 publication commit의
가시성까지 확인한다. 이번 합성 작업의 ACK만 일시적으로 HTTP 503으로 거절한 뒤 해제해
실제 Core outbox 재시도와 Revision 불변을 검증한다. 프록시는 capability URL·token·CSV body를 evidence에 저장하지 않는다.

추가 검증은 ORCH-024(같은 key/request replay·다른 쌍 충돌), 025(3개 필드 누락,
2개 교차 쌍, 3개 미지원 값), 026(descriptor와 payload 불일치 각각)을 포함한다.
부정 응답은 해당 합성 Session으로만 주입하고 실제 서비스 코드는 변경하지 않는다.
payload fault는 transport 길이·SHA-256을 맞춰 Core의 pin gate에 도달시킨다.
실패 Session의 Revision·PERSISTED ACK 0건과 기존 Book latest 보존을 확인한다.

ORCH-027은 새 `tnorm_legacycore*`/`tnorm_legacycompute*` schema에 실제 pre-pin migration을
적용하고 이번 합성 publication만 복제한다. pin이 없는 Run/dispatch/Job과 옛 request fingerprint를
준비한 뒤 실제 Core V9·Compute 0017 SQL을 실행한다. 이후 두 API를 해당 schema로 재시작해
명시적 V1/V1 HTTP status·replay와 공개 validation replay, 기존 Revision·ACK 불변을 확인한다.
실행 중인 원본 schema의 컬럼이나 trigger를 제거하지 않으며 schema·로그는 조사용으로 보존한다.

결과는 `.integration-data/trading-normalization/<runId>/evidence.json`의 `versionPin`에
시나리오별 실행 상태와 비민감 Run/Job/Revision/ACK ID로 남긴다. 재시작 전 로그는 같은 실행
디렉터리의 `before-migration-logs/`에 보존한다. ORCH-028은 두 Core 인스턴스의 admission/create
중지, claim/send 경쟁, 재시작, 불명확 create 대조, terminal·삭제·취소·기존 ACK 재시도와
격리 canary의 V1/V1 HTTP 검증 후 재개를 확인한다. 필요한 Core V10/control이 없는 조합은
PASS가 아니다. 실행별 Core·Compute schema를 새로 만들며 실패 시 중지·보류 상태를 보존한다.
V2 Core publication은 이번 범위에서 제외한다.
정확한 assertion, 공동 전환에 필요한 후속 기능과 V2 선행 구현 상태는
[결과 버전 pin E2E 문서](docs/TRADING_NORMALIZATION_RESULT_VERSION_PIN_E2E.md)를 참조한다.

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

Generate machine-readable evidence for the exact sibling revisions and normalized
OpenAPI hashes used by a local run:

```powershell
.\scripts\write-compatibility-evidence.ps1 `
  -VerificationName local-api-smoke `
  -VerificationOutcome success `
  -RequiredRepositories context,coreApi,computeApi,web,integration
```

The ignored output is written to
`.integration-data/evidence/compatibility.json`. Contract-sync and API-smoke CI
runs generate and upload the same evidence even when their verification step
fails. A detached CI checkout records a null branch while preserving the exact
commit SHA. Dirty local worktrees are reported as `dirty`; secret values and
remote URLs are never recorded.
Successful evidence generation also requires the selected repositories to be
present and the Context/Core/Compute contract hashes to match. When Web is a
required repository, its Core API and `$ref`-only Compute API snapshot hashes
must also match Context. Missing sibling checkouts are still recorded when a
failed or skipped CI run generates evidence.

`compatibility.example.yaml` remains available only for an explicitly selected
local combination that has not been exercised by the automated harness. Any
compatibility evidence records observations and never supersedes context.

## GitHub Actions contract sync

Context snapshot propagation rules, per-repository revision semantics, and
the bot PR rollout are documented in [Contract snapshot propagation](docs/CONTRACT_PROPAGATION.md).

`.github/workflows/contract-sync.yml` runs the mandatory OpenAPI and Core
document snapshot checks for integration pull requests, pushes to `main`, daily
scheduled runs, and manual dispatches. It checks out `context`, `core-api`,
`compute-api`, and `web` beside this repository using the same sibling paths as
the local harness. The OpenAPI gate verifies both Web snapshots as well as Core
and Compute snapshots. The scheduled run detects drift introduced by changes
merged directly in any sibling repository.

Before enabling the workflow for private sibling repositories, install a GitHub
App on exactly `integration`, `context`, `core-api`, `compute-api`, and `web`
with `Contents: Read` permission. Configure the integration repository with:

- Actions variable `REFINVEST_CI_APP_CLIENT_ID`
- Actions secret `REFINVEST_CI_APP_PRIVATE_KEY`

The workflow creates a short-lived token scoped to the four sibling
repositories. It does not receive OAuth credentials, database passwords, or
internal API keys. Pull requests from forks are skipped because repository
secrets are intentionally unavailable to them. Compatibility evidence records
the combined OpenAPI and Core document verification as `snapshot-sync`.

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
