"""Link completed candidate checks to their exact snapshot PR tuple.

The completion workflow runs trusted default-branch code. Downloaded artifacts
are JSON data only; they are never extracted or executed. This module never
approves, merges, or changes repository settings.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from contract_sync import ContractSyncError, REPOSITORIES

BRANCH = "automation/context-snapshots"
REMOTE_NAMES = {"coreApi": "core-api", "computeApi": "compute-api",
                "web": "web", "integration": "integration"}
CHECKS = {
    "candidate-sync": ("contract-sync-compatibility-evidence", "snapshot-sync"),
    "candidate-smoke": ("api-smoke-and-web-acceptance-compatibility-evidence",
                        "api-smoke-and-web-acceptance"),
}


def gh_json(endpoint: str, *, payload: dict | None = None):
    args = ["gh", "api", endpoint]
    if payload is not None:
        args += ["--method", "PATCH", "--input", "-"]
    environment = os.environ.copy()
    token_name = ("GH_ACTIONS_TOKEN" if "/actions/" in endpoint else
                  "GH_CONTEXT_TOKEN" if "/context/" in endpoint else "GH_TOKEN")
    if environment.get(token_name):
        environment["GH_TOKEN"] = environment[token_name]
    result = subprocess.run(
        args, input=json.dumps(payload) if payload is not None else None,
        capture_output=True, text=True, encoding="utf-8", check=False, env=environment,
    )
    if result.returncode:
        raise ContractSyncError(f"GitHub API failed for {endpoint}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def pages(endpoint: str, key: str | None = None) -> list:
    output = []
    for page in range(1, 101):
        data = gh_json(f"{endpoint}{'&' if '?' in endpoint else '?'}per_page=100&page={page}")
        entries = data[key] if key else data
        output.extend(entries)
        if len(entries) < 100:
            return output
    raise ContractSyncError("GitHub pagination limit exceeded")


def artifact_json(repository: str, artifacts: list, name: str, filename: str) -> dict | None:
    matches = [item for item in artifacts if item["name"] == name and not item["expired"]]
    if not matches:
        return None
    if len(matches) != 1 or matches[0]["size_in_bytes"] > 2_000_000:
        raise ContractSyncError(f"Ambiguous or oversized artifact: {name}")
    archive = subprocess.run(
        ["gh", "api", f"repos/{repository}/actions/artifacts/{matches[0]['id']}/zip"],
        capture_output=True, check=False,
        env={**os.environ, "GH_TOKEN": os.environ.get("GH_ACTIONS_TOKEN", os.environ.get("GH_TOKEN", ""))},
    )
    if archive.returncode:
        raise ContractSyncError(f"Unable to download artifact: {name}")
    with zipfile.ZipFile(io.BytesIO(archive.stdout)) as bundle:
        info = bundle.getinfo(filename)
        if info.file_size > 2_000_000:
            raise ContractSyncError(f"Oversized JSON: {name}")
        return json.loads(bundle.read(info))


def marker(manifest: dict) -> str:
    return f"<!-- contract-evidence:{manifest['runId']}:{manifest['runAttempt']} -->"


def validate_manifest(manifest: dict, repository: str, run_id: int, attempt: int) -> None:
    if (manifest.get("schemaVersion") != 2 or manifest.get("repository") != repository
            or manifest.get("runId") != run_id or manifest.get("runAttempt") != attempt
            or set(manifest.get("heads", {})) != set(REPOSITORIES)
            or set(manifest.get("bases", {})) != set(REPOSITORIES)):
        raise ContractSyncError("Propagation manifest identity/tuple mismatch")
    for sha in [manifest.get("contextRevision"), manifest.get("workflowSha"),
                *manifest["heads"].values(), *manifest["bases"].values()]:
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ContractSyncError("Expected immutable full commit SHAs")
    if not set(manifest.get("pullRequests", {})) <= set(REPOSITORIES):
        raise ContractSyncError("Unexpected PR repository")
    for key, pr in manifest.get("pullRequests", {}).items():
        if (type(pr.get("number")) is not int or pr["number"] < 1
                or pr.get("head") != manifest["heads"][key]
                or pr.get("base") != manifest["bases"][key]):
            raise ContractSyncError("PR identity/tuple mismatch")


def evaluate(manifest: dict, run: dict, jobs: list, evidence: dict) -> dict:
    """Only completed jobs AND matching checkout evidence can produce success."""
    expected = {"context": manifest["contextRevision"], **manifest["heads"]}
    checks = {}
    for prefix, (_, verification_name) in CHECKS.items():
        matches = [job for job in jobs if job["name"].startswith(prefix + " / ")]
        item = {"outcome": "not-recorded", "url": run["html_url"], "errors": []}
        if len(matches) == 1:
            job = matches[0]
            item["url"] = job["html_url"]
            item["outcome"] = (job.get("conclusion") or "not-recorded"
                               if job["status"] == "completed" else "pending")
        else:
            item["errors"].append("검증 job을 유일하게 식별하지 못했습니다.")
        data = evidence.get(prefix)
        item["checkedOutRevisions"] = {
            key: entry.get("revision") for key, entry in (data or {}).get("repositories", {}).items()
        }
        item["compatibilityEvidence"] = data
        if item["outcome"] == "success":
            if (not data or data.get("verification", {}).get("name") != verification_name
                    or data.get("verification", {}).get("outcome") != "success"
                    or data.get("validation", {}).get("outcome") != "success"):
                item["errors"].append("성공한 checkout 검증 증거가 없습니다.")
            else:
                actual = data.get("repositories", {})
                for key, sha in expected.items():
                    entry = actual.get(key, {})
                    if (not entry.get("available") or entry.get("revision") != sha
                            or entry.get("worktree") != "clean"):
                        item["errors"].append(f"후보 checkout 불일치: {key}")
            if item["errors"]:
                item["outcome"] = "evidence-mismatch"
        checks[prefix] = item
    outcomes = [item["outcome"] for item in checks.values()]
    if run["status"] != "completed":
        outcome = "pending"
    elif (any(job.get("conclusion") == "timed_out" for job in jobs)
          or run.get("conclusion") == "timed_out"):
        outcome = "timed_out"
    elif run.get("conclusion") == "cancelled" or "cancelled" in outcomes:
        outcome = "cancelled"
    elif run.get("conclusion") == "success" and all(value == "success" for value in outcomes):
        outcome = "success"
    else:
        outcome = "failure"
    return {"outcome": outcome, "workflowConclusion": run.get("conclusion"), "checks": checks}


def freshness(manifest: dict, context_head: str, mains: dict, prs: dict) -> list[str]:
    errors = []
    if context_head != manifest["contextRevision"]:
        errors.append("Context main이 바뀌었습니다. 새 Context SHA로 다시 전파·검증하세요.")
    for key in REPOSITORIES:
        if mains.get(key) != manifest["bases"][key]:
            errors.append(f"{key} main이 바뀌었습니다. 최신 base를 포함해 다시 검증하세요.")
        if key not in manifest["pullRequests"]:
            if mains.get(key) != manifest["heads"][key]:
                errors.append(f"{key}의 변경 없는 후보 SHA가 더 이상 최신 main이 아닙니다.")
            continue
        pr = prs[key]
        if (pr["state"] != "open" or pr["head"]["sha"] != manifest["heads"][key]
                or pr["head"]["ref"] != BRANCH or pr["base"]["ref"] != "main"
                or pr["head"]["repo"]["full_name"] != pr["base"]["repo"]["full_name"]):
            errors.append(f"{key} PR head/base/state가 바뀌었습니다. 다시 검증하세요.")
    return errors


def render_body(manifest: dict, key: str, result: dict | None = None) -> str:
    owner = manifest["repository"].split("/")[0]
    run_url = (f"https://github.com/{manifest['repository']}/actions/runs/"
               f"{manifest['runId']}/attempts/{manifest['runAttempt']}")
    status = result["outcome"] if result else "pending"
    current = result.get("freshness", "unconfirmed") if result else "unconfirmed"
    lines = [marker(manifest), "## Context source", "",
             f"원본 Context SHA: `{manifest['contextRevision']}`",
             f"이 PR의 후보 head SHA: `{manifest['heads'][key]}`", "",
             "## Candidate set", "",
             "변경이 없는 저장소도 고정 SHA로 검증합니다. 변경 묶음 전체를 검토하세요.", "",
             "| Repository | Candidate SHA | Snapshot PR |", "| --- | --- | --- |",
             f"| Context | `{manifest['contextRevision']}` | 원본 |"]
    for repo in REPOSITORIES:
        pr = manifest.get("pullRequests", {}).get(repo)
        link = (f"[#{pr['number']}](https://github.com/{owner}/{REMOTE_NAMES[repo]}/pull/{pr['number']})"
                if pr else "스냅샷 PR 연결 대기 중" if repo in manifest.get("changedFiles", {})
                else "스냅샷 변경 없음 (main 고정)")
        lines.append(f"| {REMOTE_NAMES[repo]} | `{manifest['heads'][repo]}` | {link} |")
    lines += ["", "## Verification evidence", "",
              f"후보 조합 검증: **{status}** / 최신성: **{current}**",
              f"[검증 실행 및 artifact]({run_url}) · 실행 시도 `{manifest['runAttempt']}`",
              f"워크플로 소스 SHA: `{manifest['workflowSha']}`",
              "전파 시작이나 스냅샷 복사만으로 검증 성공을 뜻하지 않습니다.",
              "완료 관찰 워크플로의 contract-candidate-evidence artifact에 최종 JSON/Markdown을 보관합니다."]
    if result:
        lines.append("")
        for name, check in result.get("checks", {}).items():
            lines.append(f"- [{name}]({check['url']}): **{check['outcome']}**")
            lines.extend(f"  - {error}" for error in check["errors"])
        lines.extend(f"- {error}" for error in result.get("freshnessErrors", []))
        if result.get("reportUrl"):
            lines.append(f"[완료 증거 artifact 및 요약]({result['reportUrl']})")
    lines += ["", "## Changed files", ""]
    lines.extend(f"- `{path}`" for path in manifest.get("changedFiles", {}).get(key, []))
    lines += ["", "## Maintainer review", "",
              "- [ ] 원본 Context diff, 이 PR head, 모든 후보 SHA와 완료 증거가 일치하는지 병합 직전에 확인했습니다.",
              "- [ ] 모든 필수 후보 검증이 success이며 최신성이 current인지 확인했습니다. pending/failure/cancelled/timed_out/미확인은 통과가 아닙니다.",
              "- [ ] OpenAPI·규범 문서·fixture의 의미와 Core/Compute/Web 구현 영향을 검토했습니다. 파일 종류로 승인하지 않았습니다.",
              "- [ ] 구현 PR, 중간 호환성, 병합 순서, 부분 실패 시 중단·복구 계획을 변경 묶음 전체에 기록했습니다.",
              "- [ ] head/base/Context 변경 시 재검증했습니다. integration main drift 결과를 후보 성공과 구분했습니다.",
              "- [ ] 필수 리뷰와 저장소 보호 규칙을 충족했으며 담당자가 수동 병합합니다.",
              "", "절차: integration의 `docs/CONTRACT_PROPAGATION.md`를 확인하세요.", ""]
    return "\n".join(lines)


def render_report(manifest: dict, result: dict) -> str:
    body = render_body(manifest, "integration", result).replace(
        "이 PR의 후보 head SHA:", "integration 후보 SHA:", 1)
    lines = [body, "## Files by repository", ""]
    for key, files in manifest.get("changedFiles", {}).items():
        lines.append(f"- {REMOTE_NAMES[key]}")
        lines.extend(f"  - `{path}`" for path in files)
    lines += ["", "## Publication status", ""]
    lines.extend(f"- {key}: {status}" for key, status in result["prUpdates"].items())
    return "\n".join(lines) + "\n"


def record_missing_tuple(repository: str, run_id: int, attempt: int,
                         run: dict, jobs: list, output: Path) -> None:
    conclusions = [job.get("conclusion") for job in jobs]
    outcome = ("timed_out" if "timed_out" in conclusions else
               "cancelled" if run.get("conclusion") == "cancelled" else "failure")
    result = {
        "schemaVersion": 2, "repository": repository, "runId": run_id,
        "runAttempt": attempt, "workflowSha": run["head_sha"],
        "outcome": outcome, "workflowConclusion": run.get("conclusion"),
        "freshness": "unconfirmed", "tupleAvailable": False,
        "contextRevision": None, "heads": None, "jobs": jobs,
        "error": "후보 manifest가 없습니다. SHA 조합의 검증 여부는 미확인입니다.",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "contract-candidate-evidence.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "contract-candidate-evidence.md").write_text(
        f"## Verification evidence\n\n실행 종료: **{outcome}**\n\n"
        f"{result['error']} 성공으로 간주하지 마세요.\n\n"
        f"[전파 실행]({run['html_url']}) · 실행 시도 `{attempt}`\n", encoding="utf-8")


def reconcile(repository: str, run_id: int, attempt: int, output: Path) -> dict:
    endpoint = f"repos/{repository}/actions/runs/{run_id}"
    run = gh_json(f"{endpoint}/attempts/{attempt}")
    if (run["path"] != ".github/workflows/contract-propagation.yml"
            or run["event"] not in ("workflow_dispatch", "repository_dispatch")
            or run["head_branch"] != "main" or run["repository"]["full_name"] != repository
            or run["status"] != "completed" or run["run_attempt"] != attempt):
        raise ContractSyncError("Only completed trusted main propagation runs may be reported")
    jobs = pages(f"{endpoint}/attempts/{attempt}/jobs", "jobs")
    artifacts = pages(f"{endpoint}/artifacts", "artifacts")
    manifest = artifact_json(repository, artifacts, f"contract-propagation-result-{attempt}",
                             "contract-propagation.json")
    if manifest is None:
        record_missing_tuple(repository, run_id, attempt, run, jobs, output)
        raise ContractSyncError("No propagation manifest: candidate verification is unconfirmed")
    validate_manifest(manifest, repository, run_id, attempt)
    if manifest["workflowSha"] != run["head_sha"]:
        raise ContractSyncError("Manifest workflow source mismatch")
    evidence = {key: artifact_json(repository, artifacts, f"{name}-{attempt}", "compatibility.json")
                for key, (name, _) in CHECKS.items()}
    result = {**manifest, **evaluate(manifest, run, jobs, evidence),
              "generatedAtUtc": datetime.now(timezone.utc).isoformat()}
    result.update(freshness="unconfirmed", freshnessErrors=["최신성은 미확인입니다."],
                  reportUrl=os.environ.get("CONTRACT_REPORT_URL"), prUpdates={})
    # Persist the result before PR writes so write failures cannot erase evidence.
    output.mkdir(parents=True, exist_ok=True)
    def save():
        (output / "contract-candidate-evidence.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "contract-candidate-evidence.md").write_text(
            render_report(manifest, result), encoding="utf-8")
    save()
    owner = repository.split("/")[0]
    try:
        prs = {key: gh_json(f"repos/{owner}/{REMOTE_NAMES[key]}/pulls/{pr['number']}")
               for key, pr in manifest["pullRequests"].items()}
        mains = {key: gh_json(f"repos/{owner}/{name}/git/ref/heads/main")["object"]["sha"]
                 for key, name in REMOTE_NAMES.items()}
        context_head = gh_json(f"repos/{owner}/context/git/ref/heads/main")["object"]["sha"]
        errors = freshness(manifest, context_head, mains, prs)
        result.update(freshness="stale" if errors else "current", freshnessErrors=errors,
                      freshnessCheckedAtUtc=datetime.now(timezone.utc).isoformat())
    except ContractSyncError as error:
        result["freshnessErrors"] = [f"최신성 조회 실패: {error}"]
        save()
        raise
    save()
    for key, pr in manifest["pullRequests"].items():
        full_name = f"{owner}/{REMOTE_NAMES[key]}"
        latest = gh_json(f"repos/{full_name}/pulls/{pr['number']}")
        # A delayed observer must not overwrite a newer propagation's PR body.
        if (latest["state"] != "open" or latest["head"]["sha"] != manifest["heads"][key]
                or latest["head"]["ref"] != BRANCH or latest["base"]["ref"] != "main"
                or latest["head"]["repo"]["full_name"] != full_name
                or latest["base"]["repo"]["full_name"] != full_name
                or marker(manifest) not in (latest.get("body") or "")):
            result["prUpdates"][key] = "skipped: PR changed or newer propagation owns body"
        else:
            try:
                gh_json(f"repos/{full_name}/pulls/{pr['number']}",
                        payload={"body": render_body(manifest, key, result)})
                result["prUpdates"][key] = "updated"
            except ContractSyncError as error:
                result["prUpdates"][key] = f"failed: {error}"
        save()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/integration", args.repository):
        parser.error("Expected owner/integration")
    result = reconcile(args.repository, args.run_id, args.attempt, args.output)
    if any(value.startswith("failed:") for value in result["prUpdates"].values()):
        raise ContractSyncError("Evidence saved, but one or more PR body updates failed")


if __name__ == "__main__":
    main()
