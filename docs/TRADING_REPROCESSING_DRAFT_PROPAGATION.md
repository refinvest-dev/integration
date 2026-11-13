# Reprocessing draft 전파 준비

integration은 Context 후보의 snapshot 분류와 E2E 선행 검사만 소유한다. 제품 의미는 Context의
[ADR-079 HTTP registry 후보](https://github.com/refinvest-dev/context/blob/78984805c262b28df098e8d79b496cb9f6fcd1ae/docs/TRADING_REVIEW_WORKFLOW_HTTP_DRAFT.md)와
[DTO 후보](https://github.com/refinvest-dev/context/blob/78984805c262b28df098e8d79b496cb9f6fcd1ae/docs/TRADING_REVIEW_WORKFLOW_DTO_DRAFT.md)가 기준이다.
이 문서는 새 endpoint/schema·정책 채택이나 업무 구현을 정의하지 않는다.

## Exact files and gate

다음 두 경로만 Core의 같은 상대 경로로 text snapshot을 분류한다.

| Context source / Core snapshot | 목적 |
| --- | --- |
| `openapi/drafts/trading-review-reprocessing.yaml` | internal 후보 |
| `openapi/drafts/trading-review-reprocessing-public.yaml` | public 후보 |

각 mapping은 단일 literal pattern이다. 두 파일과 검사기가 모두 없는 이전 Context revision은 허용한다.
source 둘 중 하나 또는 `scripts/verify-trading-review-reprocessing-draft.py`가 존재하면
`verify-contract-sync.ps1`은 다음을 순서대로 요구한다.

1. Context에 두 spec이 모두 일반 파일로 존재한다.
2. Context 검사기가 일반 파일로 존재한다.
3. Core에 두 snapshot이 모두 존재하며 source와 일치한다. 기존 text 정책에 따라 CRLF/LF만 정규화한다.
4. `python -B`로 Context 검사기를 실행해 성공한다. 실패를 후속 검사/E2E 전에 전파한다.

인접 YAML·`.backup`·하위 디렉터리 파일은 자동 포함하지 않는다. 미분류 계약 파일을 계속 거절하며
기존 Compute/Web/fixture 대상과 canonical-byte 비교는 유지한다. sibling 경로는 기존 script parameter로 받는다.
검사기·helper의 제품 규칙은 Context가 소유하며 해당 script를 Core에 snapshot으로 복제하지 않는다.
새 fixture가 필요하면 별도의 정확한 분류를 먼저 리뷰한다.

## Rollout order

1. 이 integration 분류/gate PR을 main에 반영한다. Context impact preview는 integration main을 사용하므로
   이 순서 없이 신규 YAML을 먼저 발행하면 `Unclassified Context contract files`로 실패한다.
2. Context에 두 draft와 검사기·예제/위험 변이 검증을 함께 작성하고 자체 정적 CI를 검증한다.
   draft/runtime 비활성화·registry/security/schema의 정확한 검증은 이 후속 변경의 책임이다.
3. Context merge 후 자동 Core snapshot PR에서 두 파일·외부 `$ref`·문서/marker를 검토해 반영한다.
   기존 문서 snapshot PR이 열려 있으면 최신 Context 기준으로 갱신된 diff를 확인한다.
4. 최신 main 또는 명시한 후보 SHA 조합으로 `scripts/verify-contract-sync.ps1 -IncludeWeb`을 실행한다.
   불일치가 남으면 E2E를 진행하지 않는다. 후보 조합 성공과 sibling main 성공은 별도 결과다.
5. Go는 Context 고정 링크로 연결한다. 이 mapping에 Go snapshot/`CONTEXT_REVISION`은 추가하지 않는다.

`workflow_dispatch`를 사용하면 Context/Core/Compute/Web/integration ref를 각각 고정하고 artifact의
실제 SHA·clean/result를 확인한다. 기본 sibling-main CI 실패를 후보 검증 성공으로 바꿔 보고하지 않는다.
agent는 main 직접 push·PR merge를 수행하지 않는다.

## Verification scope

회귀 명령은 `python -B -m unittest discover -s scripts -p 'test_contract_*.py'`다.
mapping 테스트는 source가 없는 revision·각 단일 파일·전체 pair의 정확한 Core 대상 및 다른 mapping 보존,
인접/backup/하위 경로 거절을 확인한다. PowerShell 테스트는 임시 checkout에서 기존 gate를 통과한 뒤
불완전 pair·검사기 부재·각 snapshot 누락/불일치·검사 실패를 검사하고 성공 시 후속 provenance gate로 진행하는지 확인한다.
성공/실패 검사기 모두 helper를 import하며 파일/bytecode를 남기지 않는지 확인한다.

테스트의 YAML과 checker는 synthetic plumbing 입력이다. 실제 Context schema의 유효성, JWT/authority,
DB/Tx·업무 API/Worker·Compute child·E2E 및 ADR-079 채택을 검증한 결과로 해석하지 않는다.
