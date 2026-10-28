# Trading Normalization Joint Cutover E2E

Context ADR-063/065/066/070, `TRADING_VERSIONING.md`의 공동 전환 및
`TR-ACC-IMPORT-ORCH-028`을 실제 HTTP/PostgreSQL 경로로 검증한다.
Core 구현과 운영 권한·절차는 [Core PR #81](https://github.com/refinvest-dev/core-api/pull/81)의
`operations/NORMALIZATION_CUTOVER.md`를 따른다. Context·Compute business code·Web은 변경하지 않는다.

## Execution

```powershell
$env:PYTHONWARNINGS = 'ignore::DeprecationWarning'
.\scripts\test-trading-normalization-result-version-pin.ps1 -CorePort 8680 -ComputePort 8681
```

Windows PowerShell에서 출력 리디렉션 시 jsonschema deprecation warning이 native error로 승격될 수
있어 위 실행은 그 경고만 억제한다. 그 외 검사 오류는 실패로 종료한다. 기존 sibling checkout/
환경 파일/timeout parameter를 그대로 받으며 Web 경로도 증적에 전달한다. 지정 포트 외에 두 번째
Core용 CorePort+10, canary용 CorePort+20/ComputePort+20/+21을 사용한다.

첫 검사는 `verify-contract-sync.ps1 -IncludeWeb`이다. loopback의 `refinvest_integration`만 허용하며
외부 credential/production/vendor 데이터 없이 기존 비민감 합성 Binance CSV를 사용한다.
실행마다 `tnorm_core<runId>`와 `tnorm_<runId>` schema를 만들고 실제 Core V1~V10 및 Compute
migration을 적용한다. Core와 Compute source root를 복제하거나 서비스 business code를 재구현하지 않는다.

두 Core는 동일한 Core schema와 DB 제어를 읽으며 Snowflake node는 0/1이다. canary는 node 2와
별도 Core·Compute schema를 사용한다. 이전 실패의 paused/unknown dispatch를 자동으로 재개하지 않는다.
실제 Core 실행 JAR을 한 번 빌드해 모든 instance/restart에 사용하고 JAR SHA-256을 증적과
process identity에 기록한다. artifact가 바뀐 기존 process는 재사용하지 않는다.
Windows worker의 fatal `10014 Bad address`만 registry별 최대 3회 같은 schema로 재기동하며
실패 로그와 recovery event를 보존한다. 일반 오류나 create 요청은 이 경로에서 재전송하지 않는다.
성공·실패 schema, 암호화 artifact와 로그는 조사용으로 보존하며 로컬 원본은 커밋하지 않는다.
각 독립 정상 publication fixture는 새 Book을 사용한다. 동일 CSV를 이미 발행된 Book에 반복 import하여
source overlap/conflict 시나리오와 혼합하지 않는다.

## Assertion mapping

`evidence.json → versionPin → scenarios → TR-ACC-IMPORT-ORCH-028 → assertions`에서 다음 항목을
추적한다. 시작은 모두 NOT_RUN이고 실제 assertion을 완료한 항목만 PASS로 바꾼다. 실패한 run은
FAIL이며 이후 미실행 항목은 NOT_RUN으로 남는다. 필수 control/조합이 없으면 실행 성공으로 처리하지 않는다.

| Item | Assertion and evidence |
|---|---|
| 01-validation-and-retry-503 | 유효한 validation과 실제 retryable Session의 retry를 두 Core에 전송하여 503, TEMPORARY_DEPENDENCY_FAILURE, retryable=true, message/traceId 검사 |
| 02-rejected-state-unchanged | 각 요청 전후 Session 전체 값, Run·dispatch·public idempotency record 전체 값과 각 table row 수 불변; 새 public key claim 0 |
| 03-zero-create-while-paused | PAUSE commit 후 원래 fleet 프록시에서 create HTTP 요청 0; 별도 schema canary traffic은 분리하여 기록 |
| 04-two-core-instances | 동일 DB/schema의 실제 Core process 두 개에서 같은 validation/retry 거절, scheduler와 DB fence 적용 |
| 05-admission-and-send-races | 실제 create를 프록시에서 잠시 멈추고 다른 Core의 admission은 Session row lock에서 대기; PAUSE가 DB advisory lock에서 기다리는 것을 확인한 뒤 두 작업 해제; PAUSE commit 뒤 새 send 없음 |
| 06-restart-and-unsent-preserved | 해당 합성 dispatch의 next_attempt_at만 연기해 미전송 준비; Core 재시작 후 paused/epoch와 dispatch 전체 값·attempt 0·key/pin 보존, 503 지속 |
| 07-terminal-deletion-cancellation-recovery | 실제 PENDING Job 취소→CANCELLED/공개 실패, Session DELETE→410, COMPLETED Job terminal polling→RESULT_READY/staging, 새 Revision/PERSISTED 0, 기존 publication ACK 실패→중지 중 재시도 완료 |
| 07 recovery | 실제 Compute가 접수한 응답을 유실시켜 UNKNOWN 보류; 기존 Compute idempotency key hash와 실제 HTTP status의 Job/dispatch/Run/token/generation/input/VersionSet/pin 대조 후 같은 Job으로 bind; create 1회, unresolved 상태 VERIFY 거절 |
| 08-failed-http-keeps-paused | 별도 canary의 실제 V1 Job descriptor에 한정된 pin fault→Core RESULT_VERSION_PIN_MISMATCH·발행 차단; 원래 fleet paused/미검증 유지, RESUME 거절 |
| 09-verified-resume-same-public-keys | 새 canary Book의 public upload/validation→실제 V1/V1 create·accepted/status/terminal/payload/replay와 독립 V1 hash·publication/ACK 확인; 그 증적 digest로 VERIFY/RESUME 뒤 중지 때 거절된 동일 public key의 validation/retry 202 |
| 10-no-duplicate-run-job-revision-ack | 재개 후 각 Run의 Job/dispatch/Revision/ACK 하나, 같은 public key 반복 요청으로 추가 생성 없음; explicit retry는 의도한 이전 실패 Run과 새 Run 두 개로 lineage 유지 |

미전송 준비의 DB scheduling delay와 scoped 프록시의 capacity/응답 유실/descriptor fault는 Integration
test fault다. Session/Run/Job/result를 합성 INSERT하여 service 실행을 대신하지 않는다. cancellation,
삭제, retryable failure, publication과 ACK는 실제 HTTP와 서비스 transaction으로 생성한다.
canary의 정상 create는 실제 Core dispatcher가 만든 새 logical request이며 무필드 요청을 허용하거나
알 수 없는 create에 새 identity를 발급하지 않는다. canary schema의 독립 HTTP 결과를 확인한 뒤에만
원래 gate를 재개한다.

## State and operational limits

정본의 중지 경계는 PAUSE transaction commit이다. 이미 permit을 잡은 admission/send는 이 commit
전에 끝날 수 있고 명령은 그 완료를 기다린다. create 응답 유실 또는 crash의 미결 요청은 자동
재전송하지 않고 보존·대조한다. 중지 중 terminal 성공을 staging까지 처리하되 새 publication과
PERSISTED outbox는 생성하지 않는다. 기존 commit의 ACK 재시도와 삭제 stale 처리는 계속된다.

테스트는 배포된 두 candidate process의 동작을 검증한다. 실제 production fleet의 이전 binary,
egress queue, 권한 부여와 배포 인벤토리는 검증하지 않으므로 production 배포 승인을 뜻하지 않는다.
운영자는 gate 미지원 이전 instance와 무필드 producer 0을 runbook에 따라 별도로 확인해야 한다.
V2 신규 선택 활성화와 V2 publication E2E는 후속 작업이다.

## Migration runner

ORCH-027의 기존 terminal Job 복제 schema에서 Compute 0017 atomic 실행을 별도 진단하고 항상
rollback한 뒤 기존 psql statement runner를 실행한다. atomic 실행의 SQLSTATE 55006은 FAIL 및
knownDeploymentLimitation으로 기록하며 해당 배포 방식의 PASS로 합산하지 않는다. 빈 canary schema의
atomic migration 성공은 기존 terminal Job의 atomic backfill 성공을 뜻하지 않는다. Core V10/공동
gate는 이 Compute SQL 문제에 의존하지 않지만, 실제 배포가 atomic runner를 사용하면 Compute
소유 후속 해결·검증까지 전환을 보류해야 한다.

## Evidence

각 run의 `.integration-data/trading-normalization/<runId>/evidence.json`에는 다섯 저장소의 정확한 commit,
시나리오별 PASS/FAIL/NOT_RUN, 비민감 Session/Run/Job/Revision/ACK ID와 row count를 남긴다.
증적은 서명 URI/token/CSV 본문을 저장하지 않는다. Core 후보 commit의 검증과 main 조합의 검증은
별도로 표시한다. 최종 실행 결과와 제한은 검증 완료 후 아래에 추가한다.


## 2026-10-28 Results

검증된 조합은 Core feature branch와 Context/Compute/Web main이다. 전체 main 조합 또는 production
전환 승인으로 해석하지 않는다. 정확한 commit과 시나리오·ID·count는
[비민감 compatibility evidence](evidence/NORMALIZATION_JOINT_CUTOVER_2026-10-28.json)에 기록했다.

| Scope | Result |
|---|---|
| OpenAPI·Core 문서·Web snapshot 및 V1/V2 fixed vector | PASS |
| Core 전체 Gradle test·ArchUnit·실제 Flyway V1~V10 | PASS |
| Core PostgreSQL store 24개 | PASS, skipped 0 |
| Integration unit 58개 / process identity 23 assertions | PASS / PASS |
| V1 publication·독립 integrity·version pin / ORCH-024~027 | PASS / PASS |
| ORCH-028 항목 01~10, 두 Core 및 격리 canary | PASS |
| 기존 terminal Job의 Compute 0017 atomic migration | FAIL, SQLSTATE 55006; atomic 배포 BLOCKED |
| 현재 전체 main의 ORCH-028 / production fleet·운영 role | NOT_RUN / NOT_RUN |
| V2 신규 Run 선택 활성화 | NOT_RUN, 범위 제외 |

이전 실패 run은 성공으로 재분류하지 않았다. 원본 DB/schema/log를 보존하고 위 JSON에 실패 원인과
run ID를 분리했다. 기존 psql statement runner의 ORCH-027 PASS는 atomic runner의 배포 통과를 뜻하지
않는다. 이전 Core instance·무필드 producer 0, production 권한 부여 및 atomic runner 해결은 운영
배포 선행 조건이며 Core/Integration PR은 저장소 정책에 따라 사람의 검토를 기다린다.
