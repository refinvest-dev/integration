# Owner Query Core→Go→Core 서비스 검증

이 harness는 Context ADR-086의 두 metadata GET만 검증한다. 제품 계약은 Context, 서비스 구현은 Core/Go가 소유한다.
기본 `main` mode는 Go의 실제 `cmd/api`와 Core의 일반 production `bootJar`를 실행한다.
명시적인 `fixture` mode는 기존 test-classpath launcher를 유지한다.
Core의 security/issuer/proxy/primary authority와 Go의 verifier/authority client/DB owner·parent·Guard를 mock하지 않는다.
일반 Main의 opt-in 조립 검증과 운영 활성화는 구분한다.

## 실행

Go module의 toolchain, Java25, Core Gradle wrapper, psql, PowerShell/pwsh, Python의 requests/cryptography가 필요하다.
Core에는 opt-in owner-query Runtime과 spool worker 중단 설정, Go에는 owner-query Main 연결이 있어야 한다.
sibling 경로는 모두 parameter로 교체할 수 있다.

```powershell
.\scripts\test-trading-owner-query-service.ps1
# 이미 빌드한 경우에도 계약 sync는 생략하지 않는다.
.\scripts\test-trading-owner-query-service.ps1 -SkipBuild -GoBinary C:\fixture\trading-review-api.exe
# 과거 명시 조립 경로를 별도로 재검증할 때만 선택한다.
.\scripts\test-trading-owner-query-service.ps1 -CoreMode fixture
```

Python CLI는 `--core-port`, `--go-port`, `--timeout-seconds`, `--output-root`, `--core-jar`도 받는다.
`-CoreJar`/`--core-jar`를 생략하면 `app/build/libs`의 유일한 bootJar를 선택한다.
manifest의 일반 Main과 test fixture class 부재를 검사한다. `-SkipBuild`는 jar/source 일치 증명이 아니므로
리뷰용 최종 실행 전에 고정 source에서 빌드하고 SHA를 함께 확인한다.
환경 파일은 읽기만 하며 process env나 서비스 저장소의 `.env`를 수정하지 않는다. 실제 DB target은 loopback의
`refinvest_integration`으로 제한하고 실행마다 `tnorm_<12hex>` schema를 소유한다. public/기존 schema는 초기화하지 않는다.

## 조립과 보호

1. `verify-contract-sync.ps1 -IncludeWeb`을 먼저 실행한다. 여기의 IncludeWeb은 snapshot 검증이며 실제 Web UI E2E가 아니다.
2. Core의 기존 migration source와 명시 authority candidate SQL을 새로운 schema에 적용한다. Flyway baseline/repair/history를 조작하지 않는다.
   격리 실행 process에서만 Flyway를 끄고 Hibernate validate를 유지한다. 운영 DB history 수용은 아니다.
3. TLS CA·서버 key pair 둘·별도 RS256 signing key와 credential·browser HS256 secret을 생성한다.
   Core 위임 private key는 Go trust file에 넣지 않는다. private 파일은 `.integration-data/owner-query-service/`에만 보관한다.
4. main mode는 일반 component scan과 strict Runtime snapshot을 사용한다. initializer·수동 Bean 주입은 없다.
   Compute/삭제/보존 worker와 `refinvest.trading-review.normalization.delivery.spool-sweep-enabled`를 끈다.
   기존 raw key 미공급과 함께 여섯 legacy worker의 Bean 부재를 실제 `/actuator/beans`에서 확인한다.
   fixture mode는 기존 명시 조립/worker 제외를 유지한다. JVM temp는 두 mode 모두 실행 폴더로 제한한다.
5. 소유한 process만 종료하고 자신이 생성한 schema만 제거한다. port 점유자를 찾아 임의 종료하거나 기존 process를 재사용하지 않는다.

Windows startup의 알려진 10014/Bad address listen 오류만 최대3회 기록 후 재시도한다. 일반 startup/HTTP/SQL/assertion 실패를
retry하지 않는다. 성공 전 시도도 evidence에 남긴다. Windows process 종료는 강제 종료일 수 있으며 graceful drain의 증거가 아니다.
Go의 drain/join·자원 종료 순서는 별도 Go 테스트/CI에서 검증한다.

## 일반 Main의 관측과 종료 fault

main mode만 loopback TLS 관측 중계기를 Go→Core authority 경로에 둔다. 고정 Core endpoint로 원 요청/body/자격을
전달하고 실제 응답/status를 돌려준다. 판정·서명·DB 조회를 대체하지 않으며 production LB 증거로 사용하지 않는다.
중계기는 traceparent·baggage 유무만 최대256건 메모리에 보관한다. JWT·subject·jti·payload는 저장하지 않는다.
양쪽 TLS 검증과 명시 CA를 유지하며 지연 fault는 하나의 callback을 최대8초 보류한다.

기존11개 시나리오 이후 별도 ACTIVE Account를 만들어 다음을 실행한다. 삭제 중인 기존 Account를 다시 열지 않는다.

- 실제 Core primary Hikari pool2개를 authority SQL lock으로 점유한 상태에서 다른 요청의503과 해제 후 재사용.
- JDBC query timeout 뒤503, 대기 SQL 부재와 해제 후 정상 조회.
- 표준 Actuator shutdown으로 새 read를 차단한 뒤 보류했던 실제 authority callback을 전달하여 기존 read200 완료.
  Runtime 완료 → Boot servlet graceful shutdown → Hikari 종료 순서와 실제 Core DB session 반환을 확인한다.
- 같은 일반 jar를 짧은 drain budget으로 다시 실행하여 진행 read503과 `Owner query shutdown incomplete`를 확인한다.
  exit0을 성공한 join으로 해석하지 않는다.

`beans,shutdown` 노출은 생성한 loopback process 설정에만 추가한다. browser JWT·CSRF cookie/header를 사용하며
보안 chain을 해제하지 않는다. 서비스의 기본 Actuator 노출 설정을 바꾸지 않는다. Core graceful 결과와 마지막 Go
소유 process 강제 정리를 별개로 기록한다. 모든 process/중계기 종료 뒤 소유 schema를 제거한다.

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

## 일반 Main 고정 실행

[2026-11-27 일반 Main 증적](../evidence/owner-query-service/2026-11-27-main.json)은 별도의16개 시나리오 PASS다.
Core `090e8e608228a5aba54974a53403480dfa9eb6e0`·Go `67e5e0df6885a2619b5c484a1f2ee5d58e7cd6cf`·harness `cc086f3f30e976bda18f3a997a61d7ccdb56b21c`에서
tracked source가 clean인 상태로 실행했다. 일반 bootJar/Go binary와 세 harness source의 SHA를 함께 기록한다.
별도의 fixture11개를 더해27개로 합산하지 않는다. 최초 개발 실행도16개를 통과했지만 실행 중 source 수정이 있어
고정 source 수용 증적으로 사용하지 않았다. 최종 실행의 짧은 drain용 Core 재기동에서 Windows10014 listen 실패가1회
발생했고 허용된 두 번째 startup에서 READY가 됐다. 실패 시도도 증적에 보존하며 원인 해결이나 무실패 실행으로 해석하지 않는다.

Core 정상 drain과 짧은 예산의 명시 incomplete를 구분해 확인했고, 두 Core 종료 후 이름으로 식별한 primary DB session이
0개임을 확인했다. 중계기·소유 process를 정리한 다음 생성한 schema를 제거했다. full production key/회원 준비,
rotation/fleet·실제 DB history·Core TR writer 인계·backup restore·운영 부하는 미실행이다.
