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

No OAuth provider credential is required for this automated API flow. OAuth
login remains a separate manual browser acceptance check.

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

## Next scope

Web browser acceptance testing can be added once the API smoke remains stable.
Web must continue to call Core only, never Compute directly.
