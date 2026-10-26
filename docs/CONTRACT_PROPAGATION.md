# Contract snapshot propagation

Context owns the contract. `scripts/contract-sync-map.json` records only how
its files are copied into sibling checkouts. It does not define product rules.

## Mechanical impact rules

| Context source | Snapshot repositories |
| --- | --- |
| `openapi/core-api.yaml` | Core, Web |
| `openapi/compute-api.yaml` | Core, Compute, Web (`$ref` resolution) |
| `AI_AGENT.md`, `docs/*.md` | Core |
| Normalization V1 fixtures | Core, Compute |
| Normalization V2 result vector | Core, Compute, integration |
| Analytics protocol fixtures | Core, Compute, integration |

`AGENTS.md`, Context verifier scripts, and the Analytics fixture verifier are
informational: they have no copied snapshot. A new file under `openapi/`,
`docs/`, or `fixtures/` must be classified before propagation proceeds. A
Context `docs/README.md` or `docs/GIT_WORKFLOW.md` would collide with a
Core-owned document and is rejected.

This classification only determines file copies. OpenAPI or normative document
changes can require implementation in Core, Compute, or Web even when that
repository has no copied file. Such work is reviewed separately.

## Local impact plan

From this repository, run:

```powershell
python -B scripts/contract_sync.py plan --revision HEAD
```

The plan compares Git blobs, not the Windows worktree representation, so
automatic CRLF conversion does not create spurious fixture changes. The plan
does not edit any checkout. Override sibling paths with `--context`,
`--core-api`, `--compute-api`, `--web`, and `--integration`.

To apply one repository's planned snapshot changes in a clean checkout, run:

```powershell
python -B scripts/contract_sync.py apply --revision <context-sha> --repository coreApi
```

The command copies mapped files, removes previously mapped documents removed
from Context, and updates that repository's `CONTEXT_REVISION`. It does not
commit or push. A PR workflow must inspect and commit the resulting diff.
The target worktree must be clean.

`CONTEXT_REVISION` records the Context commit from which each repository copied
its snapshots. The revision check accepts different SHA values when the mapped
Context files are unchanged between a repository's pinned commit and the
candidate Context commit. The existing snapshot comparisons still run before
E2E, so an old marker cannot hide changed contract content. Do not use the
impact plan alone as an E2E pass result.

## GitHub Actions rollout

The integration `contract-propagation.yml` workflow accepts a merged Context
SHA by `repository_dispatch` or manual dispatch. It opens or updates one
`automation/context-snapshots` PR per affected repository. It never merges a
PR or edits product code. It dispatches `contract-sync.yml` with the exact
candidate commit tuple, and also dispatches `api-smoke.yml` when OpenAPI or
protocol fixture snapshots change. The two verification workflows accept
explicit refs for all five checkouts. Their artifacts record tested commits.

Install the same GitHub App on `context`, `integration`, `core-api`,
`compute-api`, and `web`. The app needs Contents write and Pull requests write
on the four snapshot targets, Contents read on Context, and Actions write on
integration to start candidate checks. Store
`REFINVEST_CI_APP_CLIENT_ID` as a variable and
`REFINVEST_CI_APP_PRIVATE_KEY` as a secret in both Context and integration.
The Context push workflow needs a token with Contents write on integration
to send `repository_dispatch`. Existing read-only contract verification jobs
continue to request read-only tokens.

Before enabling the Context push trigger, run the integration propagation
workflow manually with a known merged Context SHA. Review generated PRs and
their candidate verification artifacts. A Context PR preview computes the
impact plan without write access and skips fork PRs because app secrets are
unavailable there. The Context workflow source is maintained in the Context
repository at `.github/workflows/contract-propagation.yml`.

When several repositories have PRs, review the candidate tuple first, then
merge Core, Compute, and Web changes before the integration fixture PR. The
regular integration PR checks use sibling `main` refs; the explicit candidate
checks are the evidence for the whole proposed tuple during propagation.
An integration `main` drift check can fail while those PRs remain open, and
must not be reported as a passing E2E run.
