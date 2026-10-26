"""Open or update bot-owned snapshot PRs for a pinned Context commit.

Run only in a trusted CI job with a GitHub App installation token. The script
uses the map and copy tool; it never edits product code or merges a PR.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from contract_sync import (
    REPOSITORIES, ContractSyncError, apply_plan, context_files, default_roots,
    expand_map, git, load_map, plan,
)


BRANCH = "automation/context-snapshots"
REMOTE_NAMES = {
    "coreApi": "core-api", "computeApi": "compute-api",
    "web": "web", "integration": "integration",
}


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ContractSyncError(
            f"{' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def remote_branch_exists(root: Path) -> bool:
    return bool(run("git", "ls-remote", "--heads", "origin", BRANCH, cwd=root))


def prepare_branch(
    root: Path, repository: str, allowed: set[str],
    context: Path, manifest: dict,
) -> None:
    if git(root, "status", "--porcelain"):
        raise ContractSyncError(f"Target worktree is not clean: {root}")
    run("git", "fetch", "origin", "main", cwd=root)
    if remote_branch_exists(root):
        run("git", "fetch", "origin", f"{BRANCH}:refs/remotes/origin/{BRANCH}", cwd=root)
        marker = manifest["repositories"][repository]["revision"]
        pinned = git(root, "show", f"origin/{BRANCH}:{marker}").decode("ascii").strip()
        previous = expand_map(
            manifest, context_files(context, pinned), allow_missing=True
        )
        allowed.update(
            replica.target for replica in previous if replica.repository == repository
        )
        changed = run(
            "git", "diff", "--name-only", f"origin/main...origin/{BRANCH}", cwd=root
        ).splitlines()
        outside = sorted(set(changed) - allowed)
        if outside:
            raise ContractSyncError(
                f"Bot branch contains non-snapshot files in {repository}: {outside}"
            )
        run("git", "switch", "-c", BRANCH, f"origin/{BRANCH}", cwd=root)
        run("git", "-c", "user.name=refinvest-contract-bot",
            "-c", "user.email=refinvest-contract-bot@users.noreply.github.com",
            "merge", "--no-edit", "origin/main", cwd=root)
    else:
        run("git", "switch", "-c", BRANCH, "origin/main", cwd=root)


def upsert_pr(root: Path, owner: str, repository: str, revision: str, files: list[str]) -> None:
    full_name = f"{owner}/{REMOTE_NAMES[repository]}"
    existing = json.loads(run(
        "gh", "pr", "list", "--repo", full_name, "--head", BRANCH,
        "--state", "open", "--json", "number", cwd=root,
    ))
    title = f"chore(contract): sync Context {revision[:12]}"
    body = (
        f"Context source commit: `{revision}`\n\n"
        "Mechanical snapshot propagation only. Review API and normative document "
        "changes for product-code impact before merging.\n\n"
        "Copied snapshot paths:\n"
        + "".join(f"- `{path}`\n" for path in files)
    )
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".md", delete=False
    ) as handle:
        handle.write(body)
        body_path = Path(handle.name)
    try:
        if existing:
            run("gh", "pr", "edit", str(existing[0]["number"]),
                "--repo", full_name, "--title", title,
                "--body-file", str(body_path), cwd=root)
        else:
            run(
                "gh", "pr", "create", "--repo", full_name, "--base", "main",
                "--head", BRANCH, "--title", title, "--body-file", str(body_path),
                cwd=root,
            )
    finally:
        body_path.unlink()


def publish(context: Path, revision: str, roots: dict[str, Path], owner: str) -> dict:
    manifest = load_map()
    revision = git(context, "rev-parse", "--verify", f"{revision}^{{commit}}")\
        .decode("ascii").strip()
    run("git", "fetch", "origin", "main", cwd=context)
    merged = subprocess.run(
        ["git", "-C", str(context), "merge-base", "--is-ancestor",
         revision, "origin/main"],
        capture_output=True, check=False,
    )
    if merged.returncode:
        raise ContractSyncError(f"Context commit is not merged into main: {revision}")
    replicas = expand_map(manifest, context_files(context, revision))
    for repository in REPOSITORIES:
        allowed = {
            replica.target for replica in replicas if replica.repository == repository
        }
        allowed.add(manifest["repositories"][repository]["revision"])
        prepare_branch(roots[repository], repository, allowed, context, manifest)

    impact = plan(manifest, context, revision, roots)
    changed_sources: set[str] = set()
    pull_requests: dict[str, str] = {}
    heads: dict[str, str] = {}
    for repository in REPOSITORIES:
        root = roots[repository]
        changes = impact["repositories"].get(repository, [])
        if changes:
            files = apply_plan(manifest, context, roots, impact, repository)
            changed_sources.update(change["source"] for change in changes)
            run("git", "add", "--", *files, cwd=root)
            run("git", "-c", "user.name=refinvest-contract-bot",
                "-c", "user.email=refinvest-contract-bot@users.noreply.github.com",
                "commit", "-m", f"chore(contract): sync Context {revision[:12]}", cwd=root)
        branch_diff = run(
            "git", "diff", "--name-only", "origin/main...HEAD", cwd=root
        ).splitlines()
        if branch_diff:
            run("git", "push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=root)
            marker = root / manifest["repositories"][repository]["revision"]
            upsert_pr(
                root, owner, repository,
                marker.read_text(encoding="ascii").strip(), branch_diff,
            )
            pull_requests[repository] = BRANCH
            changed_sources.update(
                replica.source for replica in replicas
                if replica.repository == repository and replica.target in branch_diff
            )
        heads[repository] = run(
            "git", "rev-parse", "HEAD" if branch_diff else "origin/main", cwd=root
        )

    result = {
        "contextRevision": revision,
        "heads": heads,
        "pullRequestBranches": pull_requests,
        "runApiSmoke": any(
            source.startswith(("openapi/", "fixtures/")) for source in changed_sources
        ),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--core-api", type=Path)
    parser.add_argument("--compute-api", type=Path)
    parser.add_argument("--web", type=Path)
    parser.add_argument("--integration", type=Path)
    args = parser.parse_args()
    roots = default_roots()
    for key, value in (("coreApi", args.core_api), ("computeApi", args.compute_api),
                       ("web", args.web), ("integration", args.integration)):
        if value is not None:
            roots[key] = value
    result = publish(args.context, args.revision, roots, args.owner)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
