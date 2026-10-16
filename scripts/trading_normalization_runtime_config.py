"""Build a local test pin from the executable Compute version registry.

This does not implement or calculate ADR-065 inputSetHash. The component hashes
are test configuration identities; the runtime contract test compares the exact
values that Core sends with those Compute persists and returns.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def build(compute_path: Path, instrument_id: str, terms_version: str) -> dict[str, object]:
    sys.path.insert(0, str(compute_path / "src"))
    from refinvest.trading_review.hashing import content_hash  # noqa: PLC0415
    from refinvest.trading_review.versions import SUPPORTED_VERSION_SET  # noqa: PLC0415

    version = SUPPORTED_VERSION_SET
    normalizer: dict[str, object] = {
        "sourceDialectVersionsByArtifactRole": {
            role.value: value
            for role, value in version.source_dialect_versions_by_artifact_role
        },
        "adapterVersion": version.adapter_version,
        "normalizerVersion": version.normalizer_version,
        "canonicalSchemaVersion": version.canonical_schema_version,
        "implementationDigest": version.implementation_digest,
    }
    normalizer["versionSetHash"] = content_hash(normalizer)
    reconstruction: dict[str, object] = {
        "instrumentTermsVersionByInstrument": {instrument_id: terms_version},
        "reconstructionPolicyVersion": version.reconstruction_policy_version,
        "reconciliationVersion": version.reconciliation_version,
        "reviewUnitSchemaVersion": version.review_unit_schema_version,
        "evidenceSchemaVersion": version.evidence_schema_version,
        "implementationDigest": version.implementation_digest,
    }
    reconstruction["versionSetHash"] = content_hash(reconstruction)
    target = {"normalizer": normalizer, "reconstruction": reconstruction}
    target["versionSetHash"] = content_hash(target)
    return target


if __name__ == "__main__":
    print(json.dumps(build(Path(sys.argv[1]), sys.argv[2], sys.argv[3]), separators=(",", ":")))
