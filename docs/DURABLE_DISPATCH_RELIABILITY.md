# Durable Dispatch and Idempotency Reliability

This harness measures correctness invariants under controlled local
concurrency and process/transport failures. It is not a throughput benchmark or
an operational SLA measurement.

## Run

```powershell
.\scripts\run-durable-dispatch-reliability.ps1 `
  -Concurrency 20 -Rounds 20 -DispatchRuns 10
```

The environment file must select exactly `refinvest_integration`. Each run
creates unique Core and Compute schemas in that database and a unique ignored
snapshot artifact directory. Existing schemas and rows are not reset, deleted,
or inferred as migration history.

Each invocation also uses profile `durable-dispatch-reliability` and a unique
`durable-dispatch-reliability-<run-id>` process registry. Its Compute API,
Worker, Core, and fault proxy cannot be discovered or reused as the default
stack. Before recovery traffic is sent, topology preflight requires the API and
Worker to have the same Compute schema/snapshot identity, the proxy to target
that API, and Core to target that proxy. Cleanup addresses only that run's
registry, including after a partial failure; it never stops a default-profile
process. If one of the bounded ports is owned by another registry or an
unrecorded process, the harness fails before starting and does not terminate
the owner.

## Assertions

- Compute HTTP idempotency: every round uses a new UUID v4 key and starts all
  requests together before the first insert. All responses must be `202`, all
  run IDs must converge, and the exact key hash must identify one PostgreSQL
  row. That owned row is deleted by exact key hash after the round so admission
  capacity does not carry into the next round.
- Payload conflict: after acceptance, the same key with a changed fee payload
  must return `409`; run ID, request payload, and fingerprint must remain
  unchanged, with no additional row.
- Durable dispatch: ten distinct PRO test members submit through the Core public
  API while Compute and its worker are stopped. Core run and dispatch rows must
  exist before Compute has any matching job. After restart, Core public polling,
  Core/Compute DB mapping, terminal state, duplicate jobs, and each kind of
  pending dispatch are checked.
- Response loss: the integration-only standard-library proxy decodes both
  fixed-length and chunked request bodies. It forwards the first POST, observes
  Compute `202`, drops that response, and requires Core to retry the same key and
  identical body. The dropped logical request must still identify one Compute
  job.

The JSON report distinguishes a PENDING Core run without a dispatch from a
PENDING dispatch whose retry time is not due, is ready, or is currently leased.
Any unexpected response, connection error, mapping mismatch, timeout, or failed
assertion makes the command fail while retaining partial ignored reports.

## Outputs and limitations

Raw JSON is stored under `.integration-data/reports/durable-dispatch-*`. Logs
remain under `.integration-data/logs`; credentials are never included in report
fields. The experiment uses a local machine, fixed non-production fixture,
single PostgreSQL instance, and ten tiny jobs. Results support only the tested
correctness invariants, not production availability, latency, capacity, or SLA
claims.
