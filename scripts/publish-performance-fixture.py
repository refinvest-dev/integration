"""Generate or idempotently publish the integration-owned performance fixture."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, cast

from performance_fixture import (
    assemble_fixture,
    derived_manifest,
    generate_artifacts,
    load_json_object,
    load_with_compute,
)
from refinvest.persistence.postgres.dataset_snapshots import (
    PostgresDatasetSnapshotRepository,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--definition", type=Path, required=True)
    parser.add_argument("--fixture-manifest", type=Path)
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--compute-api-path", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--print-derived-manifest", action="store_true")
    args = parser.parse_args()

    definition = load_json_object(args.definition)
    snapshot_path = args.storage_root.resolve() / definition["fixtureId"]
    if args.publish:
        _publish(definition, snapshot_path, args)
    else:
        assembly = generate_artifacts(definition, snapshot_path)
        actual = derived_manifest(definition, assembly, args.compute_api_path.resolve())
        _check_or_print_manifest(actual, args)
    print(definition["fixtureId"])


def _publish(definition: dict[str, Any], snapshot_path: Path, args: argparse.Namespace) -> None:
    database_url = os.environ.get("REFINVEST_DATABASE_URL")
    if not database_url:
        raise ValueError("REFINVEST_DATABASE_URL is required for fixture publication.")
    repository = PostgresDatasetSnapshotRepository(database_url)
    candidate = assemble_fixture(definition, snapshot_path)
    existing = repository.get(candidate.snapshot.snapshot_id)
    if existing is not None:
        if existing != candidate.snapshot:
            raise ValueError("Existing snapshot metadata differs; immutable fixture was not changed.")
        assembly = candidate
        load_with_compute(assembly)
    elif snapshot_path.exists():
        assembly = candidate
        load_with_compute(assembly)
        actual = derived_manifest(definition, assembly, args.compute_api_path.resolve())
        _require_expected_manifest(actual, args.fixture_manifest)
        repository.create(assembly.snapshot)
        return
    else:
        assembly = generate_artifacts(definition, snapshot_path)
        load_with_compute(assembly)
        actual = derived_manifest(definition, assembly, args.compute_api_path.resolve())
        _require_expected_manifest(actual, args.fixture_manifest)
        repository.create(assembly.snapshot)
        return
    actual = derived_manifest(definition, assembly, args.compute_api_path.resolve())
    _require_expected_manifest(actual, args.fixture_manifest)


def _check_or_print_manifest(actual: dict[str, object], args: argparse.Namespace) -> None:
    if args.print_derived_manifest:
        print(json.dumps(actual, indent=2, ensure_ascii=False))
        return
    _require_expected_manifest(actual, args.fixture_manifest)


def _require_expected_manifest(actual: dict[str, object], manifest_path: Path | None) -> None:
    if manifest_path is None:
        raise ValueError("--fixture-manifest is required unless --print-derived-manifest is used.")
    expected = load_json_object(manifest_path)
    # The recorded revision is provenance. Artifact bytes and the dependency lock are gates.
    actual_generator = cast(dict[str, object], actual["generator"]).copy()
    expected_generator = cast(dict[str, object], expected.get("generator", {}))
    actual_generator["computeRevision"] = expected_generator.get("computeRevision")
    actual["generator"] = actual_generator
    if actual != expected:
        raise ValueError("Generated fixture differs from performance/fixture-manifest.json.")


if __name__ == "__main__":
    main()
