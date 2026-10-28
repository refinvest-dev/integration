# Trading Normalization Result Version Pin E2E

이 harness는 Context ADR-065/066/070, `TRADING_VERSIONING.md`의 공동 전환 규칙과
`TR-ACC-IMPORT-ORCH-024`~`028`을 추적한다. 정본·OpenAPI·제품 business code를 변경하지 않는다.

## 실행과 소유 범위

```powershell
.\scripts\test-trading-normalization-result-version-pin.ps1 -CorePort 8680 -ComputePort 8681
```

기존 runtime/publication script와 같은 sibling 경로 parameter를 받는다.
`verify-contract-sync.ps1 -IncludeWeb`을 프로세스·DB 변경 전에 실행한다.
loopback PostgreSQL의 `refinvest_integration`만 허용하며 Core·Compute 모두 실행별
`tnorm_*` schema를 사용한다. 두 번째 Core는 같은 Core schema를, canary는 별도 schema를 사용한다.
V1 migration 복구 검증도 별도의 새 Core/Compute schema에서 수행한다.
비민감 합성 Binance fixture만 업로드하고 정상 JWT·CSRF와 실제 HTTP/DB를 사용한다.

기존 publication 시나리오의 transport 검증, 독립 hash 계산, 9개 section digest,
발행 관계 행·staging 검증, ACK 실패·재시도 및 중복 방지를 재사용한다.
새 loopback 프록시는 실제 create와 accepted를 관찰하고 ACK 요청마다 별도 DB connection으로
발행 commit의 가시성을 확인한다. V2 단위 검증을 V2 Core publication으로 해석하지 않는다.
ACK 실패는 해당 합성 Job의 요청에만 프록시가 503을 반환해 만든다. 실패 outbox와 단일 Revision을
확인한 뒤 fault를 해제하고 실제 재시도·Compute ACK 저장을 검사한다. 프로세스 중지 지연 동안
ACK가 먼저 성공할 수 있는 기존 테스트의 타이밍 경쟁을 피한다.

## 시나리오와 검증 근거

| 시나리오 | assertion |
|---|---|
| V1 pin 정상 발행 | Core Run·durable dispatch, 실제 create, Job 컬럼·request snapshot, accepted/status, descriptor/payload/digest manifest, Revision이 모두 V1/V1; input identity에 결과 버전 없음; 9개 V1 section digest; ACK 요청 전에 commit된 Revision 확인 |
| ORCH-024 | 동일 key/request는 최초 accepted와 동일한 202/Job/pin; V2/V2 요청으로 같은 key 사용 시 409 `IDEMPOTENCY_PAYLOAD_CONFLICT`; Job 전체·grant revision 불변 |
| ORCH-025 | 두 필드 누락·각 한 필드 누락은 422 `REQUEST_INVALID`; 두 교차 쌍은 422 `RESULT_VERSION_PAIR_INVALID`; schema/hash/양쪽 미지원은 422 `RESULT_SCHEMA_UNSUPPORTED`; 각 요청 뒤 Job·idempotency 수 불변 |
| ORCH-026 descriptor | 실제 Compute V1 성공 결과의 descriptor만 V2/V2로 주입; Core `PROCESSING_FAILED`/Run·dispatch `TERMINAL_FAILURE`, `RESULT_VERSION_PIN_MISMATCH`/`WAIT_FOR_RECOVERY`; Revision·ACK 0, 기존 latest 보존 |
| ORCH-026 payload | descriptor pin은 V1/V1로 유지하고 payload pin만 V2/V2로 변경; descriptor 길이·transport SHA-256은 변경 bytes와 일치시켜 pin 검증에 도달; 같은 차단 결과와 ACK 미수신 확인 |
| ORCH-027 | 새 pre-pin schema에 이번 합성 publication만 복제; pin 없는 저장 Run/dispatch/Job, 이전 logical key·fingerprint로 시작; 실제 Core V9·Compute 0017 SQL 적용; 저장 pin·request/idempotency fingerprint 일치; 양 API 재시작 후 V1 status/terminal/replay 및 public validation replay; 같은 Run·Job·Revision·ACK와 기존 hash 보존 |
| ORCH-028 | 두 Core의 503·row 불변·create 0, admission/send와 PAUSE commit 경쟁, 재시작·미전송 보존, terminal·삭제·취소·복구·기존 ACK, 격리 HTTP 실패 시 중지 유지·성공 후 같은 key 재개와 중복 0 |

ORCH-026의 fault는 새 합성 Session ID에 한정하며 status/Job pin은 실제 V1/V1을 유지한다.
원본 Job/Run pin을 수정하거나 서비스 코드에 test flag를 넣지 않는다. payload fault는 pin gate의
거절을 검증하기 위한 응답 변형이며 유효한 V2 산출물이나 V2 publication 근거가 아니다.

ORCH-027은 **저장 완료된 V1 publication**의 합성 pre-pin 복구 검증이다. 실행 중인 원본 schema를
downgrade하지 않고, 새 schema에 실제 pre-pin migration을 적용한 뒤 fixture를 채운다.
미결 create·pending 작업의 중지·대조·재개는 별도 ORCH-028 assertion으로 검증한다.

Compute 0017은 기존 `initialize-compute-schema.ps1`과 같은 `psql -f`의 statement별
autocommit 경계로 실행한다. 저장된 terminal Job이 있을 때 0017 전체를 한 transaction으로
감싸면 `0010_create_trading_review_normalization_jobs.sql:121`의 deferred Job→terminal FK 때문에
`0017_pin_trading_normalization_result_pair.sql:80`의 `ALTER TABLE trading_review_jobs`가
SQLSTATE `55006`(`ObjectInUse`)으로 실패하는 것을 별도로 재현했다. SQL을 수정하거나 constraint를
비활성화하지 않았다. 원자적 migration runner를 사용하는 배포는 Compute 소유 SQL/runner 후속 검증이
필요하며, 이 E2E의 migration PASS를 해당 배포 방식의 PASS로 해석하지 않는다.

## 이전 main의 미구현 범위 (2026-10-27 기록)

아래는 당시 기록이다. 현재 제어와 검증은 다음 절을 따른다.

Core main `0ee1053449280237dffb2fdb26124bd5a974fcc5` 기준:

- `TradingNormalizationService.kt:33,37,68`의 validation/retry 경로에는 전환 중 admission을
  `503 TEMPORARY_DEPENDENCY_FAILURE`로 거절하는 제어가 없다. public idempotency claim 이전에
  중지 여부를 확인하고 Session/Run/dispatch/idempotency를 바꾸지 않아야 한다.
- `TradingNormalizationDispatchScheduler.kt:10`의 `refinvest.compute.enabled`는 dispatch뿐 아니라
  poll/result/ACK도 함께 제어한다. 조회·terminal 처리·삭제·취소·복구를 계속하면서 새 create만
  중지하는 경계로 사용할 수 없다. dispatcher의 실제 claim/send 경계에도 fleet 공통 차단이 필요하다.
- 운영 제어는 이전 Core 인스턴스와 미전송 무필드 create가 0임을 확인하고, 응답이 불명확한 create를
  Job ID·idempotency record로 대조해야 한다. 확인 불가능한 요청은 재전송하지 말고 보류/복구해야 한다.
- 중지 상태에서 V1 작업 drain 또는 provenance 기반 backfill 뒤 명시적 V1/V1 create,
  accepted/status/terminal/replay를 격리 HTTP/DB에서 검증하고 성공한 뒤에만 admission·dispatch를
  재개해야 한다. 검증 실패 시 중지를 유지하는 rollback/runbook과 권한·감사 근거가 필요하다.
- 이후 ORCH-028 E2E에는 validation·retry 각각 503과 상태/row 불변, 새 dispatch/create 0,
  기존 terminal·삭제·취소 처리 지속, 실패 시 중지 유지, 성공 뒤 같은 public key 재사용을 추가해야 한다.

## Joint cutover implementation

현재 Core 제어와 ORCH-028의 항목별 assertion, 실행 절차·배포 제한은
[공동 전환 E2E 문서](TRADING_NORMALIZATION_JOINT_CUTOVER_E2E.md)를 따른다.

## V2 선행 구현 상태

Compute main `0701c4ac07ac89281fa6f1b263bde294b0db6452`에는 V1/V2 쌍과 V2 producer·보충 근거
경로가 있다. Core main에는 V2 streaming/staged verifier, `trading_ledger_analytics_supplement`
불변 저장 migration V8 및 Analytics input builder가 존재한다. 그러나
`TradingNormalizationService.kt:138`은 신규 admission을 V1/V1로 선택한다.
이번 검증은 이를 변경하지 않으며, 명시적 V2 선택 활성화 및 실제 보충 근거 검증·불변 저장·Core
발행 E2E는 별도 전환과 검증이 필요하다. 이 문서는 그 경로의 runtime PASS를 주장하지 않는다.

## 증적과 실패 조사

실행별 `.integration-data/trading-normalization/<runId>/evidence.json`에 checkout commit,
시나리오별 PASS/FAIL/NOT_RUN, 비민감 Session/Run/Job/Revision/ACK ID, count·hash와 로그 경로를 남긴다.
서명 locator·credential·CSV·canonical record 본문을 evidence에 기록하지 않는다.
재시작 전 로그는 `before-migration-logs/`, 기동 시도 기록은 `*-startup.json`과 보존된 로그에 둔다.
실패에는 Core Session/Run/dispatch/staging/Revision/ACK와 Compute Job의 안전한 진단을 덧붙인다.

Windows의 알려진 `10014 Bad address` 연결/기동 오류만 최대 3회 재시도한다. DB mutation은
자동 재전송하지 않으며 Compute 초기 migration은 한 연결에서 파일별 transaction과 적용 이력을
함께 commit한다. 다른 startup 오류는 즉시 실패한다. 실패한 실행은 최종 성공 실행과 별도로 보존한다.
로컬 원본 evidence·DB·환경 파일은 커밋하지 않는다.

## 2026-10-27 실행 결과

Context `06fdd057`, Core `0ee10534`, Compute `0701c4ac`, Web `2ef51669`의 원격 main과
checkout 일치를 확인했다. 위 명령은 270.687초에 종료 코드 0으로 완료됐다.
아래 PASS는 실행 가능한 V1 검증의 결과다. 공동 전환 전체의 완료를 뜻하지 않는다.

| 실행 항목 | 결과 |
|---|---|
| 계약·문서·Web snapshot 및 V1/V2 vector preflight | PASS |
| V1/V1 pin 정상 발행·commit 뒤 ACK·실패 재시도 | PASS |
| ORCH-024 / ORCH-025 | PASS / PASS: 8개 invalid create 각각 검증 |
| ORCH-026 | PASS: descriptor와 payload 각각 실패 처리·Revision/ACK 0 |
| ORCH-027 | PASS: 기존 psql runner, 양 API 재시작·HTTP replay·저장 불변 |
| 기존 V1 result integrity / publication 회귀 | PASS: 동일 verifier·publication 경로 재사용, 9개 tamper 검사 포함 |
| ORCH-028 | NOT_RUN_MISSING_CONTROL |
| V2 Core 발행 E2E | 이번 완료 범위에서 제외 |
| 추가 atomic Compute 0017 실행 | FAIL: SQLSTATE 55006; 위 runner 제약과 후속 확인 참조 |

정상 Run은 `0f033941-e41c-4a91-a854-6d1b85ca6b89`, Job은
`98781090-529a-446b-ac30-7eb161706638`, Revision은
`f29b9a74-443c-41c1-80d2-fa0b1afe99e2`, ACK outbox는
`baf1c0de-28ce-4948-afc1-d918af52ae77`이다. ACK의 실패·성공 요청 모두 별도 연결에서
발행 commit을 확인했고 Revision 1, canonical record 13, Evidence 5, ReviewUnit 1,
allocation 4, staging 0을 확인했다.

정상 실행의 원본 증적은 `.integration-data/trading-normalization/8b3ce4db1e074d658008245c94ce2d14/`의
`evidence.json`, `post-run-db-verification.json`, `before-migration-logs/`, `migration-startup/`에 있다.
descriptor fault Run/Job은 `7c955680-381f-4327-99b2-23deea062a78` /
`c6c20458-ba3e-486d-bf24-547635b6a11c`, payload fault Run/Job은
`35828726-6db3-4345-8085-9303f0f27ada` / `3f149906-ec15-49fa-aa2f-ccf8329ad3e1`이다.
두 경우 기존 latest Revision을 보존했고 PERSISTED ACK는 없다.

atomic runner 실패 원본은 `.integration-data/trading-normalization/58453b2292f54a158abae2f110abb515/evidence.json`과
`tnorm_legacycomputef593dff7dd8f` schema에 보존했다. 같은 dedicated DB의
새 `tnorm_legacycore290092f3ebfd` / `tnorm_legacycompute290092f3ebfd`에서 기존 runner의
정상 복구와 재시작을 검증했다. 초기 환경·harness 실패는 `.integration-data/pin-harness-failed-attempts.json`에
별도로 기록했으며 성공으로 재분류하지 않았다.
