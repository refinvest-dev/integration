# Trading Normalization Result Integrity runtime drift

기준 Context commit: `5803a9b3a024942ed8bcb9b5267c5972462d8e4c` (PR #33). 이 문서는 계약 정본이 아니라 snapshot 동기화 후의 구현 차이와 후속 작업 입력이다. 정본은 Context의 ADR-066, `TRADING_NORMALIZATION_RESULT_INTEGRITY.md`, OpenAPI와 fixed vector다.

## Compute

- `trading_review_payload.build_normalization_payload`는 새 `counts`, `sectionDigests`, `resultDigestManifest`, `resultSchemaVersion`, `resultHashSpecificationVersion`을 생성하지 않는다. 공개 `ReconciliationManifestPayload`의 추가 필드와 `manifestHash` 계산도 공개 DTO 기준으로 맞춰야 한다.
- `trading_review.service`의 내부 `ReconciliationManifest`/`NormalizationResult` 기반 hash는 새 공개 manifest/result hash가 아니다. section별 deterministic ordering과 canonical projection을 구현하고 Context vector로 검증해야 한다.
- terminal descriptor의 Pydantic 모델과 route는 새 필수 count/digest/version, `transportMode`를 반환하지 않는다. `payloadSha256`은 전송 entity bytes hash로 유지하고 semantic hash와 분리해야 한다.
- failure 처리와 테스트는 `RESULT_HASH_MISMATCH` 외 count, section, manifest, unsupported schema, invalid Evidence reference 등 새 code를 구분해야 한다. 기존 OpenAPI contract test는 이전 payload shape를 전제한다.

## Core

- `TradingNormalizationDispatcher`는 현재 terminal status에서 `markResultReady`까지만 진행한다. descriptor 조회, payload bytes 재조회/hash 검사, declared count와 staging 행 수 검증, section/manifest/result hash 독립 재계산이 필요하다.
- Evidence snapshot과 canonical record/allocation의 참조 무결성 및 중복 item 충돌 검사, LedgerRevision의 완료 후 publication, durable ACK 경로가 아직 없다. 새 failure taxonomy를 publication 실패에 연결해야 한다.
- 고정 Context result vector를 이용해 후속 publication 구현의 canonical bytes와 expected hash를 검증해야 한다.

## Web 및 Integration

- Web은 Core 공개 계약 snapshot만 사용하고 result hash를 계산하지 않는다. 현재 별도 API type 생성 명령이 없다.
- Integration은 Context와 Core·Compute fixture byte 일치 및 저장된 canonical bytes의 SHA-256을 확인한다. 이 검증은 production hasher의 대체물이 아니다.
