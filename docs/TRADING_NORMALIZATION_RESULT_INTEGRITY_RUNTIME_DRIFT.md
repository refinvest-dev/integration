# Trading Normalization Result Integrity runtime drift

기준 Context commit: `bd1c26d5e63b4b3ba51932df77d1510362599a79`.
이 문서는 계약 정본이 아니다. 정본은 Context의 ADR-066,
`TRADING_NORMALIZATION_RESULT_INTEGRITY.md`, OpenAPI, fixed vector다.

## 현재 검증된 경로

- Compute는 V1 `counts`, `sectionDigests`, `resultDigestManifest`, manifest/result hash와
  terminal descriptor를 생성한다. Integration result integrity runtime은 실제 terminal
  resource와 Context fixed vector의 canonical/hash 규칙을 독립 검사한다.
- Core는 terminal bytes를 수신해 streaming staging·count/section/manifest/result hash 및
  참조 무결성을 검증하고, 하나의 LedgerRevision과 관계 행을 발행한 뒤 staging을 제거한다.
- `test-trading-normalization-publication.ps1`는 synthetic Trade/Position History로
  commit된 staging identity·count, Session `ACCEPTED`, Run·dispatch `PUBLISHED`, Revision 1개, Book latest pointer,
  10개 logical count, 9개 section digest, Evidence 참조, ACK 일시 실패 후 재시도와
  중복 방지를 실제 HTTP/DB 경로에서 확인한다.
- 기존 runtime contract는 입력 identity와 `REJECTED` 경로를, result integrity runtime은
  전송·tamper·restart 경계를 각각 검증한다.

## 남은 검증

- limited-heap stress와 큰 payload의 여러 streaming staging batch 검증.
- Core/Compute crash 지점별 spool·staging·ACK recovery 전체 조합.
- V1에는 page/chunk endpoint가 없어 실제 multi-page transport 검증은 할 수 없다.
