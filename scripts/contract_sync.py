"""Plan and apply mechanical Context snapshot propagation.

This module does not infer product-code changes. It only handles paths declared
in contract-sync-map.json and fails closed for unclassified contract files.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


MAP_PATH = Path(__file__).with_name("contract-sync-map.json")
REPOSITORIES = ("coreApi", "computeApi", "web", "integration")


class ContractSyncError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False
    )
    if result.returncode:
        raise ContractSyncError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return result.stdout


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ContractSyncError(f"Unsafe mapped path: {value}")
    return path.as_posix()


def normalized(data: bytes, mode: str) -> bytes:
    if mode == "bytes":
        return data
    if mode == "text":
        return data.decode("utf-8").replace("\r\n", "\n").encode("utf-8")
    raise ContractSyncError(f"Unknown comparison mode: {mode}")


@dataclass(frozen=True)
class Replica:
    source: str
    repository: str
    target: str
    mode: str


def load_map(path: Path = MAP_PATH) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != 1:
        raise ContractSyncError("Unsupported contract map schema")
    if set(manifest["repositories"]) != set(REPOSITORIES):
        raise ContractSyncError("Contract map repository set has changed")
    return manifest


def context_files(context: Path, revision: str) -> set[str]:
    output = git(context, "ls-tree", "-r", "-z", "--name-only", revision)
    return {name.decode("utf-8") for name in output.split(b"\0") if name}


def expand_map(
    manifest: dict[str, Any], source_files: set[str], *, allow_missing: bool = False
) -> list[Replica]:
    replicas: list[Replica] = []
    destinations: set[tuple[str, str]] = set()
    mapped_sources: set[str] = set()
    for entry in manifest["mappings"]:
        if "source" in entry:
            names = [safe_path(entry["source"])]
            if names[0] not in source_files:
                if allow_missing:
                    continue
                raise ContractSyncError(f"Mapped Context source is missing: {names[0]}")
        else:
            directory = safe_path(entry["sourceDirectory"])
            files = entry["files"]
            if isinstance(files, str):
                names = sorted(
                    name for name in source_files
                    if name.startswith(directory + "/")
                    and "/" not in name[len(directory) + 1:]
                    and fnmatch.fnmatchcase(name[len(directory) + 1:], files)
                )
            else:
                names = [f"{directory}/{safe_path(name)}" for name in files]
                missing = set(names) - source_files
                if missing:
                    if not allow_missing:
                        raise ContractSyncError(f"Mapped Context sources are missing: {sorted(missing)}")
                    names = [name for name in names if name in source_files]
        for source in names:
            if source in mapped_sources:
                raise ContractSyncError(f"Context source mapped twice: {source}")
            mapped_sources.add(source)
            for target in entry["targets"]:
                repository = target["repository"]
                if repository not in manifest["repositories"]:
                    raise ContractSyncError(f"Unknown target repository: {repository}")
                destination = target.get("path") or (
                    target["directory"].rstrip("/") + "/" + PurePosixPath(source).name
                )
                destination = safe_path(destination)
                if destination in entry.get("reservedTargets", []):
                    raise ContractSyncError(f"Context source collides with local file: {destination}")
                key = (repository, destination)
                if key in destinations:
                    raise ContractSyncError(f"Duplicate replica destination: {key}")
                destinations.add(key)
                replicas.append(Replica(source, repository, destination, entry["mode"]))
    unclassified = sorted(
        name for name in source_files - mapped_sources
        if any(name.startswith(prefix) for prefix in manifest["unmappedContractPrefixes"])
        and name not in manifest["informationalFiles"]
    )
    if unclassified:
        raise ContractSyncError(f"Unclassified Context contract files: {unclassified}")
    return replicas


def read_context_blob(context: Path, revision: str, source: str) -> bytes:
    return git(context, "show", f"{revision}:{source}")


def read_repository_blob(repository: Path, target: str) -> bytes | None:
    # Compare the bytes that a PR would commit. Windows working trees may have
    # CRLF even when the repository stores an LF-only byte fixture.
    tracked = git(repository, "ls-tree", "-z", "--name-only", "HEAD", "--", target)
    if not tracked:
        return None
    result = subprocess.run(
        ["git", "-C", str(repository), "show", f"HEAD:{target}"],
        capture_output=True, check=False,
    )
    if result.returncode:
        raise ContractSyncError(
            f"Unable to read tracked snapshot {repository / target}: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return result.stdout


def plan(
    manifest: dict[str, Any], context: Path, revision: str,
    roots: dict[str, Path]
) -> dict[str, Any]:
    revision = git(context, "rev-parse", "--verify", f"{revision}^{{commit}}")\
        .decode("ascii").strip()
    if len(revision) != 40:
        raise ContractSyncError("Expected a full Context commit SHA")
    replicas = expand_map(manifest, context_files(context, revision))
    changes: dict[str, list[dict[str, str]]] = {repo: [] for repo in REPOSITORIES}
    for replica in replicas:
        expected = read_context_blob(context, revision, replica.source)
        actual = read_repository_blob(roots[replica.repository], replica.target)
        if actual is None or normalized(actual, replica.mode) != normalized(expected, replica.mode):
            changes[replica.repository].append({
                "source": replica.source,
                "target": replica.target,
                "mode": replica.mode,
                "action": "update" if actual is not None else "add",
            })
    for repository in REPOSITORIES:
        marker = roots[repository] / safe_path(manifest["repositories"][repository]["revision"])
        if not marker.is_file():
            raise ContractSyncError(f"Context revision marker is missing: {marker}")
        pinned = marker.read_text(encoding="ascii").strip()
        if len(pinned) != 40 or any(char not in "0123456789abcdef" for char in pinned):
            raise ContractSyncError(f"Invalid Context revision in {marker}: {pinned}")
        ancestry = subprocess.run(
            ["git", "-C", str(context), "merge-base", "--is-ancestor", pinned, revision],
            capture_output=True, check=False,
        )
        if ancestry.returncode:
            raise ContractSyncError(f"Context revision is not an ancestor: {marker}: {pinned}")
        if pinned == revision:
            continue
        old_replicas = expand_map(
            manifest, context_files(context, pinned), allow_missing=True
        )
        current_targets = {
            (replica.repository, replica.target) for replica in replicas
        }
        for replica in old_replicas:
            if replica.repository != repository or (
                repository, replica.target
            ) in current_targets:
                continue
            if read_repository_blob(roots[repository], replica.target) is not None:
                changes[repository].append({
                    "source": replica.source,
                    "target": replica.target,
                    "mode": replica.mode,
                    "action": "delete",
                })
    return {
        "contextRevision": revision,
        "repositories": {repo: entries for repo, entries in changes.items() if entries},
    }


def apply_plan(
    manifest: dict[str, Any], context: Path, roots: dict[str, Path],
    result: dict[str, Any], repository: str,
) -> list[str]:
    if repository not in REPOSITORIES:
        raise ContractSyncError(f"Unknown target repository: {repository}")
    actions = result["repositories"].get(repository, [])
    if not actions:
        return []
    root = roots[repository].resolve()
    if git(root, "status", "--porcelain"):
        raise ContractSyncError(f"Target worktree is not clean: {root}")
    changed: list[str] = []
    for action in actions:
        target = root / safe_path(action["target"])
        if not target.resolve().is_relative_to(root):
            raise ContractSyncError(f"Target path escapes repository: {target}")
        if action["action"] == "delete":
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read_context_blob(
                context, result["contextRevision"], action["source"]
            ))
        changed.append(action["target"])
    revision_path = root / safe_path(manifest["repositories"][repository]["revision"])
    revision_path.write_text(result["contextRevision"] + "\n", encoding="ascii")
    changed.append(revision_path.relative_to(root).as_posix())
    return changed


def default_roots() -> dict[str, Path]:
    integration = Path(__file__).resolve().parent.parent
    workspace = integration.parent
    return {
        "coreApi": workspace / "api" / "refinvest",
        "computeApi": workspace / "compute-api" / "refinvest",
        "web": workspace / "web" / "web",
        "integration": integration,
    }


def verify_revisions(
    manifest: dict[str, Any], context: Path, candidate: str,
    roots: dict[str, Path], include_web: bool = False,
) -> dict[str, str]:
    candidate = git(context, "rev-parse", "--verify", f"{candidate}^{{commit}}")\
        .decode("ascii").strip()
    target_replicas = expand_map(manifest, context_files(context, candidate))
    selected = list(REPOSITORIES) if include_web else [
        repo for repo in REPOSITORIES if repo != "web"
    ]
    revisions: dict[str, str] = {}
    for repository in selected:
        marker = roots[repository] / safe_path(manifest["repositories"][repository]["revision"])
        if not marker.is_file():
            raise ContractSyncError(f"Context revision marker is missing: {marker}")
        pinned = marker.read_text(encoding="ascii").strip()
        if len(pinned) != 40 or any(char not in "0123456789abcdef" for char in pinned):
            raise ContractSyncError(f"Invalid Context revision in {marker}: {pinned}")
        ancestor = subprocess.run(
            ["git", "-C", str(context), "merge-base", "--is-ancestor", pinned, candidate],
            capture_output=True, check=False,
        )
        if ancestor.returncode != 0:
            raise ContractSyncError(f"Context revision is not an ancestor: {marker}: {pinned}")
        if pinned != candidate:
            pinned_replicas = expand_map(
                manifest, context_files(context, pinned), allow_missing=True
            )
            source_at_pin = {
                (replica.source, replica.target, replica.mode)
                for replica in pinned_replicas if replica.repository == repository
            }
            source_at_candidate = {
                (replica.source, replica.target, replica.mode)
                for replica in target_replicas if replica.repository == repository
            }
            if source_at_pin != source_at_candidate:
                raise ContractSyncError(
                    f"Context source set changed for {repository} since {pinned}"
                )
            for source, _target, mode in source_at_candidate:
                before = read_context_blob(context, pinned, source)
                after = read_context_blob(context, candidate, source)
                if normalized(before, mode) != normalized(after, mode):
                    raise ContractSyncError(
                        f"Context source changed for {repository} since {pinned}: {source}"
                    )
        revisions[repository] = pinned
    return revisions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "apply", "verify-revisions"])
    parser.add_argument("--context", type=Path, default=Path(__file__).resolve().parents[2] / "context")
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--core-api", type=Path)
    parser.add_argument("--compute-api", type=Path)
    parser.add_argument("--web", type=Path)
    parser.add_argument("--integration", type=Path)
    parser.add_argument("--include-web", action="store_true")
    parser.add_argument("--repository", choices=REPOSITORIES)
    args = parser.parse_args()
    roots = default_roots()
    for key, argument in (("coreApi", args.core_api), ("computeApi", args.compute_api),
                          ("web", args.web), ("integration", args.integration)):
        if argument is not None:
            roots[key] = argument
    manifest = load_map()
    if args.command == "plan":
        result = plan(manifest, args.context, args.revision, roots)
    elif args.command == "apply":
        if not args.repository:
            parser.error("apply requires --repository")
        impact = plan(manifest, args.context, args.revision, roots)
        result = {
            "contextRevision": impact["contextRevision"],
            "repository": args.repository,
            "changed": apply_plan(
                manifest, args.context, roots, impact, args.repository
            ),
        }
    else:
        result = verify_revisions(
            manifest, args.context, args.revision, roots, args.include_web
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
