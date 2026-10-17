# Trading Normalization Result Integrity runtime test

`scripts/test-trading-normalization-result-integrity.ps1`는 Context ADR-066의 공개 결과를
실제 Core public upload/validation → durable HTTP dispatch → Compute worker → terminal
resource 경로에서 검증한다. Integration의 Python verifier는 test 전용이며 Core/Compute
runtime에서 가져오거나 호출하지 않는다.

## 실행

```powershell
.\scripts\test-trading-normalization-result-integrity.ps1
```

`-ContextPath`, `-CorePath`, `-ComputePath`, `-WebPath`, `-EnvironmentFile`,
`-CorePort`, `-ComputePort`, `-TimeoutSeconds`를 지정할 수 있다. script는 먼저
`verify-contract-sync.ps1 -IncludeWeb`, Context fixed vector self-test, 기존
Runtime Identity Contract Test를 통과해야 result integrity runtime을 실행한다.
기본 DB 이름은 `refinvest_integration`이며 다른 DB는 기존 하네스의 safety gate에서
거부한다. 각 실행은 별도 Compute schema와 `.integration-data/trading-normalization/<runId>`
evidence를 사용한다. 실패 시 evidence와 로그를 보존한다.

## 검증 경계

Context V1은 page/chunk API 없이 `SINGLE_RESOURCE` terminal resource 하나를
전송한다. verifier는 `Accept-Encoding: identity` 요청의 response entity body bytes를
JSON parsing 전에 descriptor `payloadSha256` 및 `byteSize`와 비교한다. 그 뒤 Context
OpenAPI의 descriptor와 payload schema를 검증하고, 공개 item을 stable ID로 수집·정렬해
10개 declared count, 9개 section digest, reconciliation manifest hash, result digest
manifest, overall resultHash를 독립 재계산한다. Evidence → canonical record → Core
Artifact/source row와 allocation/match/issue reference를 확인한다.

단일 resource를 논리적 page 0으로 다뤄 동일 bytes 재수신, 다른 bytes 충돌, resource
누락을 검사한다. 수집 item 순서 뒤집기, 동일 item 재수신, item 누락과 9개 메모리
tamper scenario도 검증한다. Compute API를 controlled restart한 뒤 descriptor와 exact
resource bytes, semantic digest를 다시 확인한다. 실제 multi-page 및 page-size 변경은
V1 endpoint가 없어 이 runtime에서 실행하지 않는다.

`evidence.json`에는 commit, ID, count, hash prefix, scenario 결과, 시간과 로그 경로를
남긴다. API key, JWT, signed locator, raw CSV 및 canonical record 본문은 기록하지 않는다.
Core terminal staging, Session `ACCEPTED`, LedgerRevision publication은 이 테스트의
대상이 아니다.
