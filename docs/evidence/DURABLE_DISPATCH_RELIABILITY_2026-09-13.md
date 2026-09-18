# Durable Dispatch Reliability Evidence — 2026-09-13

This is a controlled local correctness experiment, not a production benchmark
or SLA measurement.

## Environment

- Database: `refinvest_integration`
- Core schema: `ri_rel_core_20260913015736_d9f06d`
- Compute schema: `ri_rel_compute_20260913015736_d9f06d`
- Core revision: `8ef6132e12ca7db750687be09b78eeeb6ad27c74`
- Compute revision: `55b377c01a5825036da413eeda1fc284fb30e27c`
- Integration revision: `576db76496be713fd35265722a3b2c149b7d57e9`
- Execution completed: `2026-09-12T17:00:32.8933341Z` (`2026-09-13` Asia/Seoul)

## Command

```powershell
.\scripts\run-durable-dispatch-reliability.ps1 `
  -Concurrency 20 -Rounds 20 -DispatchRuns 10 -RecoveryTimeoutSeconds 180
```

## Results

- Same-key concurrency: 20 rounds × 20 requests = 400 actual HTTP requests;
  400 returned `202`, 20 logical jobs were created, duplicate jobs 0, 5xx 0,
  connection errors 0.
- Changed-payload conflicts: 20 attempts; `409` responses 20, mutated existing
  jobs 0, additional jobs 0.
- Compute-down recovery: Core runs submitted 10, pre-recovery durable dispatches
  10, pre-recovery Compute jobs 0. After restart, terminal/COMPLETED runs 10,
  unique Compute jobs 10, duplicate jobs 0, mapping mismatches 0, orphan PENDING
  0. No retry-not-due, ready, or leased PENDING dispatch remained. Recovery took
  30.484 seconds from Compute restart initiation to completed observation.
- Response loss: one Compute `202` was intentionally dropped after acceptance.
  Core made one same-key, identical-body retry; that logical request produced one
  Compute job and zero duplicates.
- Validation: OpenAPI sync passed before and after; isolated Compute PostgreSQL
  integration tests passed 6/6. The existing Core-to-Compute API smoke also
  passed after the reliability run, including completed same/cross-market runs,
  deterministic replay, and the expected terminal calendar failure.

Machine-readable evidence is in the ignored local report
`.integration-data/reports/durable-dispatch-20260913015736_d9f06d/result.json`.
All requested scenarios were executed successfully in this run.

## Resume bullet draft

Built and executed a real HTTP/PostgreSQL reliability harness for durable
backtest dispatch: verified 400 concurrent idempotent submissions converged to
20 jobs with zero duplicates, 20/20 payload conflicts returned 409 without
mutation, and 10/10 Core jobs recovered after Compute restart in 30.484 seconds;
also confirmed a dropped post-acceptance response retried to one Compute job.
