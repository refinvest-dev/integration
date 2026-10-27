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

integration `contract-propagation.yml`은 `main`에서만 실행하며,
`repository_dispatch` 또는 수동 dispatch로 **현재 Context main의 전체 40자리 SHA**를
받는다. 과거 main의 조상 SHA도 현재 main과 다르면 거부한다. 정보성 파일만 바뀌어도
새 Context SHA로 다시 검증하는 보수적인 정책이다.

영향 저장소마다 `automation/context-snapshots` PR을 생성·갱신한다. 전체 후보 조합을
먼저 확정한 뒤 PR 본문에 Context SHA, 해당 PR head, 변경 없는 저장소를 포함한
Core/Compute/Web/integration SHA, 관련 PR, 변경 파일과 담당자 체크리스트를 기록한다.
제품 코드, 자동 승인, 자동 병합, 보호 규칙 우회 또는 LLM 병합 판정은 포함하지 않는다.

검증은 별도 실행을 dispatch하고 끝내지 않고, 같은 전파 실행의 재사용 워크플로 job으로
연결한다. `candidate-sync` 성공 후 `candidate-smoke`를 실행한다. 두 job 모두 다섯
저장소를 **고정된 후보 SHA**로 checkout한다. 규범 문서만 변경되거나 스냅샷 PR이
없는 경우에도 동일하게 검사한다. E2E 시작 전 계약·문서·fixture 검사는 유지한다.
일반 PR/push/schedule 및 개별 수동 검증 경로도 유지한다.

## Evidence and outcomes

| 결과물 | 내용 |
| --- | --- |
| 원래 전파 실행의 `contract-propagation-result-<attempt>` | `contract-propagation.json`: 원본 Context, 네 후보 head, 네 base main, 실제 PR 번호/head, 변경 파일, 실행 ID/attempt, 워크플로 소스 SHA. 처음 상태는 `pending`이다. |
| 각 검증 job의 `*-compatibility-evidence-<attempt>` | `compatibility.json`: 실제 checkout SHA와 worktree, 계약 hash, 검사 단계의 outcome. 단계 증거만으로 전체 job 성공을 뜻하지 않는다. |
| 완료 관찰 실행의 `contract-candidate-evidence-<run-id>-<attempt>` | 최종 JSON과 Markdown: 전체 후보, 실제 checkout 증거, job 링크와 conclusion, 전체 outcome, 최신성 조회, PR 갱신 결과. 실행 Summary와 PR에서 연결한다. |

`contract-evidence.yml`은 전파 실행이 종료된 뒤 `workflow_run: completed`로 별도 실행된다.
취소·시간 초과로 원래 실행의 마지막 단계가 사라져도 완료 job의 GitHub API conclusion을
조회한다. 종료된 **동일 run ID/attempt**의 artifact만 사용한다. 기본 브랜치의 신뢰된
코드만 실행하며, 다운로드한 ZIP에서는 정해진 JSON만 읽고 압축 파일이나 후보 코드를
실행하지 않는다. Context 읽기, PR 본문 쓰기, Actions artifact 읽기 토큰은 분리한다.

| Outcome | 해석 |
| --- | --- |
| `pending` | 시작·대기·실행 중. 검증 성공이 아니다. |
| `success` | 전파 실행과 두 검증 job이 모두 완료·성공했고, 두 artifact의 성공 증거와 실제 다섯 checkout SHA 및 clean worktree가 후보와 일치한다. 의미상 승인이나 병합 허가가 아니다. |
| `failure` | 실패·skipped·job 식별 실패·성공 증거 누락 또는 불일치. 각 check의 원래 conclusion과 오류를 함께 확인한다. |
| `cancelled` | 실행 또는 필수 job이 취소됨. 앞선 단계의 성공 증거로 대체하지 않는다. |
| `timed_out` | GitHub가 필수 job 또는 실행을 시간 초과로 종료함. 일반 실패/취소와 구분한다. |

최신성은 별도 필드다. `current`는 **완료 관찰 시점**에 Context main, 각 base main,
모든 열린 PR head/base가 후보와 일치했다는 뜻이다. `stale`이면 과거 성공을 재사용하지
않는다. API 조회 실패는 `unconfirmed`이며 성공으로 취급하지 않는다. 두 job 중 하나가
시간 초과이고 다른 job이 취소된 경우 전체 outcome은 `timed_out`이며 개별 conclusion도 남긴다.

manifest가 만들어지기 전 취소·실패했거나 artifact가 만료/누락되면 후보 증거는 미확인이다.
이때도 완료 job을 조회할 수 있으면 종료 결과를 최종 JSON/Markdown에 남기고 후보 SHA는
`null`, `tupleAvailable: false`로 표시한다. 관찰 워크플로는 실패로 종료하며 PR을 갱신하지 않는다.
관찰 실행의 실패 Summary/로그를 확인하고 새 전파를 시작한다. PR의 `pending`이 계속
남아도 통과가 아니다. 원래 실행의 초록 표시만으로 관찰 실행의 성공과 최신성까지
확인했다고 해석하지 않는다. 관찰 시점 이후 변경을 상시 감시하거나 PR 병합을
서버에서 차단하는 구현은 아니다.

재검증은 현재 Context main SHA로 **새 전파 실행**을 시작하는 것이 원칙이다.
GitHub의 실패 job만 재실행하면 이전 attempt의 manifest를 재사용하지 않으므로 증거가
미확인으로 남을 수 있다. 모든 job을 재실행하거나 새 전파를 수행한다. 늦게 끝난 관찰자가
새 전파의 PR 본문을 덮어쓰지 않도록 run/attempt 본문 표식과 PR head를 다시 확인한다.

## Maintainer merge procedure

1. 관련 PR을 하나의 변경 묶음으로 모은다. Context 원본 diff, 각 스냅샷 PR과 구현 PR,
   변경 없는 저장소 SHA도 함께 검토한다. 문서만 Core에 복사되어도 Compute/Web 의미에
   영향이 있을 수 있다. OpenAPI·규범 문서·fixture를 파일 종류만으로 자동 승인하지 않는다.
2. 담당자가 의미 변경(호환성, 요청/응답, 상태·오류, persistence/dispatch, 버전·hash,
   fixture/테스트 범위)을 검토한다. 추가 구현이 필요하면 각 서비스 소유 저장소에서
   리뷰한다. 계약·제품 규칙 변경은 Context 정본부터 다룬다. 봇 브랜치에 구현을 섞지 않는다.
3. 구현이 필요한 경우 기존/새 계약 모두와 호환되는 구현 PR을 먼저 서비스 main에 반영하고
   다시 전파한다. 단일 안전한 병합 순서가 없는 breaking change는 병합을 중단하고 Context에
   버전 분리·점진적 전환 방식을 먼저 마련한다. smoke 성공만으로 의미 검토를 생략하지 않는다.
4. 병합 **직전** PR API의 `head.sha`, `base.ref`, Context `main` SHA, 네 서비스/integration
   `main` SHA를 최종 JSON의 `heads`, `contextRevision`, `bases`와 대조한다. 예:

   ```powershell
   gh api repos/refinvest-dev/core-api/pulls/<number> --jq '{head: .head.sha, base: .base.ref, state: .state}'
   gh api repos/refinvest-dev/context/git/ref/heads/main --jq .object.sha
   gh api repos/refinvest-dev/core-api/git/ref/heads/main --jq .object.sha
   # compute-api, web, integration의 main 및 변경 묶음의 나머지 PR도 같은 방식으로 조회한다.
   ```

   head, base main, Context가 달라지면 기존 성공 증거를 재사용하지 않는다. rebase, main 병합,
   충돌 해결, 새 구현 커밋 모두 새 후보 SHA다. Context는 관련 내용뿐 아니라 어떤 새 커밋이든
   재전파한다. API를 조회할 수 없거나 artifact가 없으면 확인되지 않은 상태로 중단한다.
5. 두 필수 후보 검증의 최종 `success`, 최신성 `current`, 동일 run/attempt와 실제 checkout
   SHA를 확인한다. 저장소 자체 CI·필수 리뷰·보호 규칙도 별도로 충족해야 한다. 모든 검토
   항목과 중간 상태 호환성·복구 계획을 PR에 기록한 후 담당자가 **수동 병합**한다.

## Multi-repository merge and recovery

변경 묶음의 대표 PR에 아래 기록을 남긴다. PR 본문은 봇이 갱신하므로 담당자의 상세
검토·체크리스트 완료 기록은 별도 PR 댓글 또는 연결한 문서에도 보관한다.

| 항목 | 담당자가 기록할 내용 |
| --- | --- |
| 변경 묶음 | Context SHA, 모든 스냅샷/구현 PR, 다섯 후보 SHA, run/attempt와 최종 증거 링크 |
| 순서와 중간 상태 | 각 단계의 old/new Core·Compute·Web 조합, 배포 영향, API/규범 의미 호환성, 확인 책임자 |
| 각 병합 | 직전 검증 링크, 실제 merge commit SHA, 시각, 남은 PR과 main SHA |
| 실패·복구 | 실패 로그/DB 근거, 중단 시점, 이미 병합한 범위, revert 또는 후속 수정 PR, 복구 검증 링크 |

기본 순서는 선행 호환 구현 → Compute/Core(의존성에 따라 순서를 명시) → Web → integration
fixture다. 구현 의존성에 따라 다른 순서를 선택할 수 있지만 **각 중간 상태의 호환성**을 먼저
증명해야 한다. GitHub의 여러 저장소 병합은 원자적이지 않다. 전체 후보 조합 검증 성공은
중간 main 조합이나 실제 배포 순서의 성공 증거가 아니다.

하나를 병합하면 base/main 조합이 바뀐다. 실제 merge/squash SHA와 남은 PR head를 포함하도록
새 전파를 시작하고 다시 후보 검증을 완료한 후 다음 PR을 수동 병합한다. 앞선 묶음의 성공을
새 main SHA에 그대로 붙이지 않는다. 이 규칙은 일부 저장소만 변경된 경우에도 같다.

부분 병합·push·PR 갱신·후속 CI 실패 또는 예상 밖 drift가 발생하면 남은 병합을 즉시
중단한다. 이미 성공한 병합을 전체 성공으로 기록하지 않는다. 원래 run/attempt artifact,
서비스 로그와 integration DB 상태, 실제 각 main/PR SHA를 보존한다. 이미 병합한 변경이
호환되면 담당자 검토를 거친 후속 수정 PR로 전진 복구하고, 그렇지 않으면 역순 revert PR을
검토한다. 자동 revert·강제 push·보호 규칙 우회를 수행하지 않는다. 계약 의미를 바꿔야 하면
Context에서 먼저 수정한다. 복구 후 전체 후보 검증과 최종 main 조합의 일반 CI를 다시 확인한다.
integration 이외 DB의 destructive operation은 수행하지 않는다.

일반 integration PR/push/schedule 검사는 sibling `main`을 사용한다. Context가 먼저 변경되어
스냅샷 PR들이 남아 있는 동안 **integration main drift 실패는 실제 실패로 유지**한다. 후보
조합이 성공해도 main drift 실패를 성공으로 바꾸거나 숨기지 않는다. 반대로 main의 초록
표시도 열린 PR 후보 조합을 검증한 결과가 아니다. 마지막 병합 후 main 검사까지 성공했을 때
묶음의 완료를 기록한다.

## Automated safeguards and human responsibility

자동으로 검사하는 것은 Context의 현재 main/전체 SHA, 깨끗한 작업 트리, 계약 map 분류,
이전/현재 mapped 경로와 revision marker만 허용하는 봇 브랜치 diff, push 직전 파일 제한과
main 최신성, 일반 fast-forward push, PR head/base 정합성, 같은 run/attempt의 완료 job과
실제 checkout 증거다. merge 충돌·예상 밖 파일·동시 push는 중단하며 force push하지 않는다.

제품 의미·구현 영향·중간 호환성·병합 직전 최신성 재조회·필수 리뷰 충족·병합/복구 결정은
담당자가 확인한다. `current`는 조회 시점의 결과이며 조회와 PR 본문 PATCH 사이 또는
이후의 변경을 원자적으로 잠그지 않는다. 변경 묶음의 동시 수정을 조율하고 병합 직전
다시 조회해야 한다. 현재 구현은 서비스 PR SHA에 별도 required check/status를 게시하지
않으므로 PR 본문은 보호 규칙을 대체하지 않는다.

## GitHub App and rollout

Install the same GitHub App on `context`, `integration`, `core-api`,
`compute-api`, and `web`. The app needs Contents write and Pull requests write
on the four snapshot targets and Contents read on Context. Candidate checks
now use reusable workflows, so propagation no longer requests Actions write.
The completion observer uses the integration `GITHUB_TOKEN` with Actions read
for completed jobs/artifacts, a Contents read/Pull requests write target token,
and a separate Contents read Context token. Store
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

`contract-evidence.yml`은 기본 브랜치에 있어야 완료 이벤트를 받는다. 이 변경을 integration
main에 반영한 뒤 새 전파를 실행한다. 최초에는 trigger 활성화 전 성공·실패·취소·시간 초과,
head 변경, 새 Context, 일부 저장소만 변경되는 실제 봇 PR 경로를 확인한다. 로컬 mock 및
bare Git remote 테스트는 GitHub App 설치, Actions job 이름, artifact 전달, PR 쓰기의 운영
검증을 대신하지 않는다. 새 워크플로 소스를 후보 integration SHA의 소스와 구분해 기록한다.

설정 조회 결과, 권장 보호 설정과 적용 순서는
[`CONTRACT_PROPAGATION_SETTINGS.md`](CONTRACT_PROPAGATION_SETTINGS.md)를 확인한다.
이 구현에서 GitHub 저장소 설정을 변경하지 않는다.
