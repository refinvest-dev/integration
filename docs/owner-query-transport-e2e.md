# Owner Query 실제 Main body/취소 검증

기존 ADR-086의 Account/Book 단건 GET과 Core PCP-07/08을 검증한다. 일반 Core bootJar와 Go Main,
실제 browser JWT·RS256 위임·primary authority·Go owner/parent Guard를 사용한다.
metadata TLS hop에서 실제 Go 응답의 framing/본문 전달만 제어하며 authority 판정은 변경하지 않는다.

```powershell
.\scripts\test-trading-owner-query-service.ps1 -CoreMode transport
# 고정 source에서 미리 빌드한 경우에도 contract sync는 생략하지 않는다.
.\scripts\test-trading-owner-query-service.ps1 -CoreMode transport -SkipBuild -CoreJar C:\fixture\app.jar -GoBinary C:\fixture\trading-review-api.exe
```

환경·경로 parameter와 새 소유 schema/키 생성·삭제 규칙은 [공통 검증](owner-query-service-e2e.md)을 따른다.
default main·기존 fixture/fleet mode는 유지한다. E2E 전에 `verify-contract-sync.ps1 -IncludeWeb`을 실행하며
IncludeWeb은 snapshot 검사다. 실제 Web UI 실행은 별도 CI 결과를 따른다.

## 실제 경로와 fault

```text
Core Main → metadata fault TLS relay → Go Main → authority TLS observer → Core Main
```

Core request3초·Go request2초·authority1초·Core maxInFlight1을 명시한 fixture다. 운영 기본값이나 SLO가 아니다.
일반 Core runtime·여섯 legacy worker 부재와 두 정상 metadata 응답을 먼저 확인한 뒤 다음 fault를 순서대로 적용한다.

| fault | 확인할 결과 |
|---|---|
| 정상 header/본문 prefix 뒤 대기 | Core 전체 deadline504, 동시에 들어온 다음 요청503·Go 미도달, Core 쪽 소켓 EOF/reset, slot 재사용 |
| 원 Content-Length에 못 미치는 본문 | public502·fallback 없음, 다음 정상 조회 성공 |
| Content-Length16385 | 실제 본문 전체를 보내기 전 header 상한 거절502·Core 소켓 종료·slot 반환 |
| 길이 없는 chunked16385 byte | 실제 Go JSON 뒤 whitespace만 추가해 JSON 의미를 유지. 16KiB 수신 cap 거절502·소켓 종료·slot 반환 |
| browser disconnect 중 미완료 body | request budget에 의해 body 대기가 제한되고 transport/slot 정리. 즉시 취소/장기 job 취소로 해석하지 않음 |
| 정상 Spring drain 중 body 대기 | 고정 draining 로그 뒤에도 첫 요청이 pending임을 확인한 후 실제 본문 완료·200·종료 순서 검증 |
| 짧은 drain budget | 진행 요청503·Core 소켓 종료·incomplete ERROR. process exit0을 성공 join으로 해석하지 않음 |

fault는 명시적으로 arm한 다음 성공 metadata 응답 하나에만 적용한다. 이후 정상 요청은 원래 응답을 받는다.
deadline/상한/강제 drain에서 Core의 peer EOF/reset이 fixture release **전에** 관측돼야 한다.
fixture가 자체 대기15초를 마쳐 연결을 닫는 것을 Core 취소 증거로 사용하지 않는다. guard 만료나 다른 I/O 오류는 실패다.
이는 한정 fixture의 실제 소켓 관측이며 JVM thread/heap·전체 OS 자원/SLO의 증명으로 확대하지 않는다.

## source와 정리

- [전송 시나리오](../scripts/owner_query_transport_e2e.py): 기존 Main 조립 재사용·slot/HTTP/정상/강제 종료 관측.
- [fault 제어](../scripts/owner_query_body_fault.py): one-shot·framing·미완료 body·peer close·제한된 대기와 비민감 상태.
- [TLS 전달](../scripts/owner_query_fleet_relay.py): 기존 fixed loopback relay. 선택하지 않으면 기존 경로를 그대로 전달한다.
- [안전 검사](../scripts/test_owner_query_transport_e2e.py): authority에 fault 주입 금지·one-shot·실제 truncation·유효 JSON인 overflow.

본문은 실제 검증 후 수신한 bounded Go 응답이며 원 위임·credential·Member identity를 evidence/log에 넣지 않는다.
Core signing private key는 Go에 공급하지 않는다. `.env`·example은 변경하지 않는다.
소유 fault/relay handler·process 정리 뒤 자신이 만든 integration schema만 제거한다. Go cleanup은 Windows에서 강제 종료일 수 있다.
Core worker 부재는 fixture 검사이며 실제 Core HTTP command/외부 writer 중단은 별도 인계 조건이다.
기존 일반 Main16개·fleet13개와 이번 전송 검증을 하나의 완료 숫자로 합산하지 않는다.


## 고정 실행과 발견한 회귀

[2026-11-28 증적](../evidence/owner-query-service/2026-11-28-transport.json)은9개 시나리오 PASS와7개 one-shot fault,
peer-close/release 순서·정상/강제 종료·소유 process/relay/schema 정리를 기록한다.
Core `97248c5087a45ec7f72c997d373f379a5f28ac64`·Go `e4086b355df2cd8e29e5518c27b86645de3f491e`·harness `7da419fe62c2ba4044f9a216073b11c5b67599fc`의
tracked source가 clean인 상태에서 실행했고 실제 binary/source SHA를 고정했다.

첫 고정 실행은4개 checkpoint 이후 선언 길이 초과 header의 public502 뒤에도 peer 전송이 닫히지 않아 실패했다.
Core BodyHandler가 예외를 던지는 경로를 명시적 subscription 취소로 바꾸고 실제 TLS 회귀를 추가했다.
Core 대상25개·안전 검사13개 통과 뒤 위 고정 source에서 전체를 다시 실행했다. 첫 실패와 당시 Windows startup 재시도도
별도로 보존하며 성공 실행으로 합산하지 않는다. 구/신 다른 binary fleet·운영 부하/heap·실제 인계는 미실행이다.
수정 후 중간 실행의 종료 단계 ConnectionError와 Windows listen 재시도 뒤 정상 조회503도 실패로 남겼다.
두 실패의 원인을 확정하지 않으며, 마지막 PASS 실행과 별도로 `intermediateFailures`에 고정한다.
