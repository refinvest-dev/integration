# Owner Query key·credential fleet 검증

Context ADR-086·Core PCP-12를 따르는 격리 service harness다. 일반 Core bootJar 2개와 Go cmd/api 2개를
같은 loopback `refinvest_integration`의 새 소유 schema에서 실행한다. 모든 node는 같은 service build를 사용하며
서로 다른 불변 trust/config snapshot을 순차 적용한다. 구버전 binary·운영 fleet 수용과 구분한다.

## 실행과 소유권

```powershell
.\scripts\test-trading-owner-query-service.ps1 -CoreMode fleet
# source와 산출물을 미리 고정해 빌드한 경우
.\scripts\test-trading-owner-query-service.ps1 -CoreMode fleet -SkipBuild -CoreJar C:\fixture\app.jar -GoBinary C:\fixture\trading-review-api.exe
```

기본 `main`의16개 검증과 기존 `fixture` mode는 유지한다. 인자·환경 파일·DB 격리·계약 sync 요구는
[공통 서비스 검증](owner-query-service-e2e.md)을 따른다. `-IncludeWeb` sync는 UI 실행이 아니다.
키/credential·browser secret·CA/keystore는 실행마다 생성해 private local 폴더에 둔다.
Core 위임 private key는 Go snapshot에 넣지 않는다. `.env`·example은 읽기만 하며 수정하지 않는다.
실제 DB migration history·회원 backfill·업무 writer 인계는 실행하지 않는다.

| source | 책임 |
|---|---|
| [fleet 조립](../scripts/owner_query_fleet_e2e.py) | node별 snapshot·실제 Main·worker 부재 확인·순차 교체·metadata로 확인 후 routing 등록 |
| [교체 시나리오](../scripts/owner_query_fleet_scenarios.py) | key/양방향 credential overlap·VERIFY_ONLY·철회 commit·잘못된 입력·retirement |
| [TLS relay](../scripts/owner_query_fleet_relay.py) | 고정 loopback origin·제한된 metadata/authority path 전달, fixture membership과 비공개 memory slot |
| [안전 검사](../scripts/test_owner_query_fleet_e2e.py) | SSRF/path 제한·미확인 node 제외·명시 diagnostic 분리·token slot 상한/clear |

## 실제 요청 경로와 검증 의미

```text
Browser JWT → Core A/B → metadata TLS relay → Go A/B
                                          → authority TLS relay → Core A/B primary authority
                                          → Go owner/parent Guard → metadata response
```

relay는 원 credential·위임·JSON body와 실제 status/응답을 전달한다. 판정·서명·DB 결과를 만들어내지 않는다.
transport의 연결 종료 header만 지정해 순차 교체 중 오래된 upstream connection 재사용을 피한다.
TLS CA 검증은 양쪽에서 유지한다. callback 단일 hop의 최종 authority는 선택된 실제 Core다.
각 정상 matrix는 Core ingress2 × Go2 × Core authority2의8조합에 Account/Book 두 resource를 요청한다.
16개 정상 응답뿐 아니라 실제 metadata/authority hop 각각16개를 확인해 legacy 성공으로 대체되지 않게 한다.

membership은 **fixture의 명시 routing 제어**다. 새 node는 실제 metadata 확인 후 등록하고 교체 전에 제외한다.
초기4개 node와 의도적으로 준비가 덜 된 node는 diagnostic 경로에서만 먼저 확인한다. `readyz=200`이더라도
새 kid를 모르는 node는 정상 serving 선택에서 제외된다. 실제 LB·DNS·node inventory/배포 ack를 구현하거나
확인한 것은 아니다. 행렬은 순차 요청이며 운영 처리량/동시성 시험으로 해석하지 않는다.

## 순차 적용과 실패 검사

1. 구 key·구 자격의 초기 fleet에서 두 metadata 경로를 확인한다.
2. Core authority에 새 public key와 callback credential overlap을 먼저 공급한다. Go에는 ingress 자격 overlap을 준비하고,
   serving Go의 새 key 검증을 확인한다. 새 key를 모르는 Go는 제외한 상태에서 diagnostic 요청의 내부401/public502와
   authority 미호출을 확인한다. 준비된 snapshot으로 재시작·실제 요청 확인 뒤 serving에 복귀시킨다.
3. Core A는 새 signing key/ingress 자격, Core B는 구 signing key/자격을 쓰게 한다. Go A/B도 서로 다른 callback 자격을
   사용한다. 두 방향 overlap과 서로 다른 설정 세대의8조합을 실제 경로로 확인한다.
4. 마지막 구 signing Core를 drain/종료한 뒤 새 key로 재시작한다. 그 전에 실제 Core가 발급한 아직 유효한 구 token을
   Go 두 개와 Core authority 두 개에서 재검증해 VERIFY_ONLY를 확인한다. token은 private memory에만 둔다.
5. 동작 중 파일을 손상시켜도 준비된 process의 snapshot이 바뀌지 않는지 확인한다. 같은 손상 입력의 신규 Core/Go
   process는 의도한 설정 오류로 종료되고 READY가 되지 않아야 한다. 기존 정상 fleet은 계속 응답해야 한다.
6. Member·원 caller·구 kid·특정 jti의 권위 차단 commit 전후를 확인한다. Member/caller의 재허용은 명시 fixture SQL이며
   runtime의 자동 복원 기능이 아니다. 구 kid는 철회한 뒤 다시 허용하지 않는다. jti 보관은 exp+leeway까지 잡는다.
7. 실제 시계로 fixture의 TTL60·양방향 skew 각5·가장 긴 request budget12를 고려한 창을 기다린다.
   구 issuer 종료 시각도 하한으로 삼는다. 이 수치는 검증 fixture의 보수적 대기이며 고정 운영 retirement 값이 아니다.
8. Go를 새 ingress 자격·새 callback 자격·새 public key만 가진 상태로 교체한 뒤, Core에서도 구 callback 자격/public key를
   회수한다. 구 자격401·새 자격 정상, 새 위임 matrix 성공과 구 key의 새 공격 서명이 authority 호출 전에 거절되는지 확인한다.

VERIFY_ONLY는 issuer가 발급을 중단한 상태이며 탈취된 구 private key의 새 서명까지 자동으로 차단하는 장치가 아니다.
fixture의 공격 서명은 구 key를 가진 공격자를 모사한다. 검증자가 구 public key를 보유하고 authority가 kid를 허용하면
fresh 서명은 검증될 수 있으므로, 긴급 철회는 현재 primary authority 차단 commit과 추가 발급 중단·전파를 함께 수행한다.
모든 serving Core가 발급을 중단한 것과 fixture의 공격자 negative probe를 구분한다.

## 자원·증적과 남는 조건

Core rolling replacement는 새 ingress/authority routing에서 제외한 뒤 인증·CSRF를 거친 표준 Spring shutdown을 사용한다.
고정 complete 로그와 실제 process 종료를 확인한다. 이번에는 교체 중 active read drain을 다시 시험하지 않으며
그 근거는 별도의 일반 Main 검증이다. Go의 소유 process 정리는 Windows에서 강제 종료이며 graceful join 증거가 아니다.
마지막에 relay·소유 process 종료를 확인한 뒤 자신이 만든 schema만 제거한다.

token slot은 최대4096 byte 위임 하나를 메모리에만 보관하고 끝나면 clear한다. jti hash는 fixture authority DB에만 저장하며
원 token/credential·subject/jti/hash를 log/trace/evidence에 넣지 않는다. 공개 증적은 code/binary/source hash·node label/설정 세대·
matrix와 checkpoint·startup 시도·정리 결과만 담는다. 키·환경·원 로그/keystore는 커밋하지 않는다.

실제 secret 공급/폐기·보호 mount/ACL·TLS 인증서/CA 교체·clock-skewed host·구/신 binary 호환성·production routing·
긴급 fleet 전파/미확인 node 완전 차단·DB history/회원 초기화·Core TR 단일 writer 인계·backup restore·운영 부하는 별도다.
기본 health-only·proxy 비활성화와 운영 전환 gate는 유지한다. 선택된 두 metadata GET의 증거를 쓰기/Worker에 확대하지 않는다.
