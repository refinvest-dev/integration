# Trading Analytics V1 fixed vectors

These synthetic, non-sensitive vectors pin ADR-069. `input-identity-v1.json` contains two independently byte-hashed artifact samples (derived from the **unchanged** Normalization V1 public vector), exact AnalysisConfig, semantic identity, two grant variants and two runtime variants. `result-integrity-v1.json` is a separate result-projection vector with all three typed Metrics, an insufficient sample, an actual `0/1`, exclusion and prior baseline memberships, a loss→entry relation, holding distribution and empty Finding section. Its `referenceCatalog` is fixture-only cross-reference data and is **excluded** from logical result hashes; it is not a replacement for a real pinned Core LedgerRevision.

Expected SHA-256 values:

| Projection | Expected hash |
|---|---|
| AnalysisConfig | `cb99a38fd6d1506e6b6d1fc91fafee0b40b13ec946219eac8abeee06ea1ce1af` |
| Analytics semantic input | `fd11891b4aa4086c5b520cf947809ea9d165372aafe6ee99589a3ec45bc69851` |
| Analytics result digest manifest | `29fc9f6665cfe683c8d1a17be8292ab756a3057306738a06e3f6c752ecc4ff5d` |

The per-Metric and per-section expected hashes are in `result-integrity-v1.json`. Run `node fixtures/trading-analytics/verify-v1.mjs` from Context to independently recompute artifact byte sizes/SHA-256, manifest/config/input/Metric/section/evidence/result hashes, structural counts and reference catalog. The verifier uses a separate JavaScript canonicalizer and Node crypto; all vector hash inputs use ASCII strings and safe JSON integers, where its sorted-key serialization equals RFC 8785 JCS. Python `jsonschema` validates the two `.schema.json` files. Consumer implementations must independently recalculate these vectors using their production RFC 8785 implementation, not copy the expected values into results.

The two grant variants have different URI/expiry, and the two runtime variants different attempt/deletion generation; both share exactly the same `inputIdentity` root and expected semantic hash. The runtime tuples are unequal. Existing Normalization V1 files and hash specifications remain unchanged.
