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

The start scripts record only the processes they launch in the ignored
`.integration-data/processes.json` registry. Use the lifecycle helpers to
inspect or stop those recorded processes:

```powershell
.\scripts\status-integration.ps1
.\scripts\stop-integration.ps1
# Or stop only a recorded service.
.\scripts\stop-integration.ps1 -Service core-api
```

`stop-integration.ps1` verifies the recorded launcher PID and start time
before stopping its process tree. It never stops a listener or worker that was
not recorded by an integration start script.

Wait for both health endpoints, then seed the test Member and execute the API
smoke flow.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8080/actuator/health

$token = .\scripts\seed-core-member.ps1
.\scripts\run-api-smoke.ps1 -AccessToken ($token | Select-Object -Last 1)
```

The smoke uses normal HS256 Access JWT verification plus a CSRF cookie/header
pair. It requires Strategy creation, StrategyVersion creation, Compute
dispatch, and a `COMPLETED` result with `datasetSnapshotId=integration-e2e-v1`.

## Web and authentication acceptance

Use the same `localhost` host for Web and Core. With the integration default
`SameSite=Lax`, `localhost` and `127.0.0.1` are different sites, so a Web app
on `localhost` cannot reliably send Core cookies at `127.0.0.1`.

```powershell
# Starts Web with NEXT_PUBLIC_API_BASE_URL=http://localhost:8080 by default.
.\scripts\start-web.ps1
```

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

Web browser acceptance testing can be added once the API smoke remains stable.
Web must continue to call Core only, never Compute directly.
