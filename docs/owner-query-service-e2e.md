# Owner Query Core→Go→Core 서비스 검증

이 harness는 Context ADR-086의 두 metadata GET만 검증한다. 제품 계약은 Context, 서비스 구현은 Core/Go가 소유한다.
Go의 실제 `cmd/api`와 Core의 실제 RefinvestApplication을 전용 test-classpath launcher로 실행한다.
Core의 security/issuer/proxy/primary authority와 Go의 verifier/authority client/DB owner·parent·Guard를 mock하지 않는다.
Core production entrypoint/Bean 공급·운영 활성화와 fixture launcher를 구분한다.

## 실행

Go module의 toolchain, Java25, Core Gradle wrapper, psql, PowerShell/pwsh, Python의 requests/cryptography가 필요하다.
Core에는 `:app:prepareOwnerQueryFixture`와 test launcher, Go에는 owner-query Main 연결이 있어야 한다.
sibling 경로는 모두 parameter로 교체할 수 있다.

```powershell
.\scripts\test-trading-owner-query-service.ps1
# 이미 빌드한 경우에도 계약 sync는 생략하지 않는다.
.\scripts\test-trading-owner-query-service.ps1 -SkipBuild -GoBinary C:\fixture\trading-review-api.exe
```

Python CLI는 `--core-port`, `--go-port`, `--timeout-seconds`, `--output-root`도 받는다.
환경 파일은 읽기만 하며 process env나 서비스 저장소의 `.env`를 수정하지 않는다. 실제 DB target은 loopback의
`refinvest_integration`으로 제한하고 실행마다 `tnorm_<12hex>` schema를 소유한다. public/기존 schema는 초기화하지 않는다.

## 조립과 보호

1. `verify-contract-sync.ps1 -IncludeWeb`을 먼저 실행한다. 여기의 IncludeWeb은 snapshot 검증이며 실제 Web UI E2E가 아니다.
2. Core의 기존 migration source와 명시 authority candidate SQL을 새로운 schema에 적용한다. Flyway baseline/repair/history를 조작하지 않는다.
   fixture Core만 Flyway를 끄고 Hibernate validate를 유지한다.
3. TLS CA·서버 key pair 둘·별도 RS256 signing key와 credential·browser HS256 secret을 생성한다.
   Core 위임 private key는 Go trust file에 넣지 않는다. private 파일은 `.integration-data/owner-query-service/`에만 보관한다.
4. Core TR/Compute worker를 끄고 실제 인증·중계/authority capability를 fixture에서 명시 조립한다.
   무조건 등록되는 spool sweeper는 fixture에서 제외하고 JVM temp도 실행 폴더로 제한한다.
5. 소유한 process만 종료하고 자신이 생성한 schema만 제거한다. port 점유자를 찾아 임의 종료하거나 기존 process를 재사용하지 않는다.

Windows startup의 알려진 10014/Bad address listen 오류만 최대3회 기록 후 재시도한다. 일반 startup/HTTP/SQL/assertion 실패를
retry하지 않는다. 성공 전 시도도 evidence에 남긴다. Windows process 종료는 강제 종료일 수 있으며 graceful drain의 증거가 아니다.
Go의 drain/join·자원 종료 순서는 별도 Go 테스트/CI에서 검증한다.

## 수용과 증적

- principal 위임·큰 ID·Account5/Book9 DTO·위조 identity header 무시, foreign/absent·ADMIN의 소유권 제한.
- browser401·CSRF403·잘못된 입력400, Member 차단 commit 후403/재허용·positive cache 없음.
- key 차단·authority 미준비503과 별개인 Go readiness, archived metadata·현재 parent 삭제410.
- Core servlet thread4개에서8개 동시 callback을 완료하는 한정 fixture. 운영 처리량/SLO가 아니다.
- 자신의 TR table에 둔 ACCESS EXCLUSIVE fault로 Go 요청 deadline504와 browser disconnect 후 bounded SQL 정리를 확인한다.
  승인되지 않은 durable job 취소나 즉시 철회를 의미하지 않는다.
- Go credential snapshot을 restart로 바꿔 internal401→public502와 legacy DB fallback 부재를 확인한다.
- trace child/trace ID·baggage 비전달, 조회에 따른 idempotency/Import row 미생성, owned process/schema cleanup.

`evidence.json`에는 source revision·Go binary SHA·pass/fail·시도·소유 process·정리 결과만 기록한다.
환경/키/keystore·원 JWT·subject/jti/hash·거래 payload를 artifact에 올리지 않는다. 로그도 private local 산출물이다.
운영 회원 초기화/backfill·권한/키 교체/철회·신뢰 ACL·실제 DB history/ID·Core TR writer 인계·backup restore·전체 부하 수용은 별도다.

## 확인한 실행

[2026-11-27 payload-free 요약](../evidence/owner-query-service/2026-11-27.json)에 최종 격리 서비스 시나리오11개 PASS와
source/binary hash·정리 범위를 기록했다. 첫 sync120초 timeout과 후속 Core TLS probe 실패는 성공으로 계산하지 않는다.
sync600초·소유 process tree 정리/단계 기록, fixture CA/leaf subject 분리·PKCS12 alias·probe 원인 기록을 보완한 뒤 통과했다.
초기 기능 수용 뒤 spool sweeper 제외·JVM temp 격리를 추가하고, 세 저장소의 고정 commit에서 다시 실행했다.
최종 증적은 Go23c1216·Coree6f13be·integration5211525와 실제 binary/source SHA를 기록하며 세 working tree는 clean이었다.

integration CI의 기존 authority 부정 검사는 ADR-086 후 바뀐 오류 문구 대신 과거 `unadopted`를 기대해 실패했다.
기대 문구를 현재 Context verifier에 맞췄으며 runtime flag를 true로 바꾸는 mutation이 실제로 거절되는 조건은 유지한다.
기존 API/Web smoke와 새로운 서비스 E2E를 같은 검증으로 합산하지 않는다.
