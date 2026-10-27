# Contract propagation settings review

## Read-only inspection

2026-10-27에 `refinvest-dev/{context,core-api,compute-api,web,integration}`의
repository, `branches/main/protection`, `rulesets?includes_parents=true` REST API를
익명 GET으로 조회했다. 로컬에 `gh`와 인증 세션이 없어 관리자 인증 조회는 하지 못했다.
설정, 보호 규칙, App 설치 또는 PR을 변경하지 않았다.

| 항목 | 관찰 결과 | 해석 |
| --- | --- | --- |
| Context repository/rulesets | 404 | 비공개 또는 접근 불가 여부를 구분할 수 없다. 미확인. |
| Core/Compute/Web/integration repository | 200, 공개 저장소, 기본 브랜치 main | 조회 가능한 기본 정보만 확인. |
| main branch protection (다섯 저장소) | 401 | 인증 필요. 리뷰/필수 체크/관리자 적용/bypass는 미확인. |
| Core/Compute/Web/integration rulesets | 공개 조회에서 빈 배열 응답을 확인했지만 전체 적용 정책은 미확인 | branch protection과 인증 범위 밖 정책의 부재를 뜻하지 않는다. 아래 운영 확인에서 인증 후 재조회한다. |
| 자동 병합 허용, App 권한·설치, CODEOWNERS 적용, required checks | 미확인 | 공개 repository 응답만으로 실제 병합 권한이나 정책을 추정하지 않는다. |

## Recommended settings and application order

아래는 관리자가 검토·적용할 권장 사항이며 이 작업에서 적용하지 않았다.

1. 관리자 read 권한으로 다섯 저장소의 branch protection, 조직 포함 ruleset, bypass actor,
   App 설치 범위·권한, Actions 허용·secret/variable, auto-merge 허용 여부를 조회하고 보관한다.
   기존 required check 이름과 리뷰 정책을 먼저 확인한다. 조회할 수 없는 항목은 미확인으로 남긴다.
2. Context와 네 대상 main에 PR 필수, 담당자 승인 1명 이상, CODEOWNERS 리뷰, 새 push 시
   기존 승인 무효화, 최신 push 작성자 이외의 승인, conversation resolution을 권장한다.
   관리자·App에도 적용하고 봇을 bypass 목록에 넣지 않는다. force push와 브랜치 삭제를 막는다.
   CODEOWNERS의 실제 팀/담당자는 관리자가 지정한다. 자동 승인·자동 병합은 사용하지 않는다.
3. 각 저장소의 실제 PR head에서 실행되는 기존 서비스 CI와 계약/문서 검사를 required로 설정한다.
   적용 전에 실제 check context 이름을 확인한다. 이 변경은 기존 검증 job 이름을 유지한다.
   후보 검증은 integration의 실행에 연결되며 서비스 PR head에 check-run을 게시하지 않는다.
   따라서 `candidate-sync` 같은 이름을 서비스 required check로 바로 등록하면 영구 대기가
   될 수 있다. PR 본문과 외부 후보 증거 확인은 담당자의 수동 절차로 유지한다.
4. integration main에 이 워크플로와 관찰 코드를 반영한다. App은 Context Contents read,
   대상 Contents/Pull requests write를 사용하고, 관찰 토큰은 Contents read/Pull requests write,
   integration `GITHUB_TOKEN`은 Actions read로 제한한다. Context dispatch에 필요한 integration
   Contents write는 기존 Context 측 토큰에 유지한다. App의 설치 권한 자체를 줄이는 것은
   다른 워크플로 사용처를 확인한 후 별도 변경한다. 이 구현은 토큰 요청에서 Actions write를 제거한다.
5. Context 자동 trigger를 활성화하기 전에 현재 Context main SHA로 수동 전파한다. 실제 봇 PR의
   생성/갱신, 다섯 고정 checkout, run/attempt artifact, 재사용 workflow job 이름, 종료 관찰,
   한국어 본문, 체크리스트와 담당자 리뷰를 확인한다. 실패·취소·시간 초과도 실제로 확인한다.
   `workflow_run` 관찰자가 기본 브랜치에 배포되어 있는지와 artifact 보존 기간을 확인한다.
6. PR head 변경, 새 Context, 대상 main 변경, 부분 병합 후 재전파로 기존 성공을 재사용하지
   않는지 확인한다. 한 저장소만 변경되는 경우와 늦은 관찰자의 덮어쓰기 방지도 점검한다.
   보호 규칙이 봇의 직접 main push와 리뷰 없는 병합을 막는지 관리자 확인을 남긴다.
7. 위 확인을 완료한 뒤 Context 자동 dispatch를 활성화한다. 이후 main drift와 후보 검증을
   구분해 운영하고, 실패 시 나머지 병합을 중단하고 변경 묶음 단위로 기록·복구한다.

## Remaining operational verification

실제 GitHub App 토큰의 checkout/push/PR 쓰기, 재사용 워크플로 호출, 완료 관찰자의 권한,
API job 이름과 timeout conclusion, artifact 다운로드, 보호 규칙 적용은 로컬 테스트로
확인되지 않았다. 실제 봇 PR 경로를 실행하기 전에는 **운영 검증 완료로 표시하지 않는다**.
보고서가 오래되면 권한·보호 설정을 다시 조회한다.
