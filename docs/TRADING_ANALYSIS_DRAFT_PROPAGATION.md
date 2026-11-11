# Analysis draft 전파 준비

integration은 두 Context draft의 exact Core snapshot 분류와 E2E 선행 검증만 소유한다.
제품 의미/schema는 Context ADR-078과 후속 Analysis HTTP 문서/검증기가 정본이다.

- `trading-review-analysis-read.yaml`, `trading-review-analysis.yaml`만 Core text snapshot으로 분류한다. 인접 YAML/backup wildcard는 허용하지 않는다.
- source가 없는 이전 Context는 optional이다. 두 spec 또는 checker 하나라도 있으면 source 3개·Core spec 2개 전체와 Context checker 성공을 요구한다.
- 검증은 `python -B`로 실행하며 실패를 E2E 전에 전파한다. runtime route/view/scope 활성화나 제품 ACK/계산 규칙을 소유하지 않는다.
- 새 spec의 Context PR preview 전에 이 mapping을 main에 반영한다. Core 문서 snapshot #129도 별도 선행 반영 대상이다.
- main snapshot 불일치와 candidate 검증을 구분한다. workflow_dispatch에서 Context/Core/Compute/Web/integration ref를 각각 고정하고 실제 artifact SHA/clean/result를 기록한다.
- Context main 반영 후 snapshot PR의 두 spec/외부 refs·최신 docs marker를 검토한다. Core 문서/계약 sync 통과 전 E2E를 진행하지 않는다.

로컬 회귀: `python -B -m unittest discover -s scripts -p 'test_contract_*.py'`.
E2E 선행 검사: `scripts/verify-contract-sync.ps1 -IncludeWeb`.
Analysis 새 업무/권위/SQL/부하/운영 이관 수용은 이 gate 통과와 별도다.
