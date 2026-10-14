# Trading Normalization Identity runtime drift

기준 Context commit: `636b6c1eaad2f83e8b56c014e13d34ee79087210` (PR #32). 이 문서는 계약의 정본이 아니라 snapshot 동기화 직후의 구현 차이를 기록한 후속 작업 목록이다.

## Core

- `TradingNormalizationService.NormalizationInputFingerprint.hash`는 이전 list 기반 `artifactsByRole`과 전체 VersionSet을 직렬화한다. V1의 정확한 9개 root 필드, role-keyed object, normalization에 적용되는 nested VersionSet, `hashSpecificationVersion`, UTF-16 key/ID 정렬을 구현해야 한다.
- `RestClientTradingNormalizationClient.body`는 flat `targetVersionSet`, `artifacts`, `expectedInputHash`를 전송한다. 새 `inputIdentity`, `artifactAccessGrants`, `expectedInputSetHash` 계약과 grant/identity 분리를 적용해야 한다. `coreNormalizationRunId`와 `retryPolicyVersion`은 이미 요청에 있으나 새 wire 구조에서 보존 여부를 재검증해야 한다.
- status 파서와 dispatcher는 기존 `expectedInputHash`/`versionSetHash`를 기준으로 비교한다. hash specification version, exact input identity, terminal identity와 분리된 failure code를 검증해야 한다.
- 기존 Kotlin 테스트는 고정 Context vector로 production hasher를 검증하지 않는다. fixture의 저장된 `canonicalJson`, `canonicalUtf8Hex`, `expectedSha256`을 독립 oracle로 사용해야 한다.

## Compute

- Pydantic create/update DTO, job admission, persistence와 status는 기존 `expectedInputHash`/flat VersionSet 계약을 사용한다. V1 identity schema를 엄격히 검증하고 입력 hash를 독립 재계산하며 `coreNormalizationRunId`를 재시작·idempotent 조회까지 보존해야 한다.
- normalization engine의 내부 `input_hash`는 Core ADR-065 identity와 다른 필드 집합으로 계산된다. 이를 `expectedInputHash`와 직접 비교하는 경로는 새 `expectedInputSetHash` 계약과 맞지 않는다. source bytes SHA-256, input set hash, result/manifest hash failure를 구분해야 한다.
- terminal payload builder는 `coreNormalizationRunId`와 V1 identity/hash specification version을 반환하지 않는다. status, descriptor, payload와 ACK 검증의 새 필드를 구현해야 한다.
- 기존 OpenAPI contract test와 route test는 이전 request/response shape를 전제로 한다. 고정 vector와 새 schema로 갱신해야 한다.

## Web 및 Integration

- Web은 Core 공개 OpenAPI snapshot만 소비한다. 브라우저에서 identity hash를 계산하지 않는다. 현재 표준 API type 생성 명령은 없으며 UI/runtime 코드는 이 동기화에서 수정하지 않았다.
- Integration은 이번 변경으로 Context commit, OpenAPI snapshot, Core·Compute fixture byte 일치를 검사한다. runtime compatibility E2E는 후속 구현 작업에서 검증한다.
