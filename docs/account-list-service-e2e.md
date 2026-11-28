# Account 목록 Core→Go→Core Main 검증

Context ADR-087의 Account 목록만 대상으로 한다. 제품 계약은 Context, 구현은 Core/Go, 이 저장소는 조립·fixture·실행 증적을 소유한다.
일반 production Core bootJar와 실제 Go `cmd/api`를 실행하며 test initializer나 인증·authority·SQL mock을 사용하지 않는다.
이 모드는 기존 metadata Main/fleet/transport 시나리오와 별도로 집계한다.

## 실행과 보호

```powershell
.\scripts\test-trading-owner-query-service.ps1 -CoreMode account-list
```

경로·환경 파일·Java/Go 실행물 인자는 [기존 서비스 안내](owner-query-service-e2e.md)와 같다.
`verify-contract-sync.ps1 -IncludeWeb`을 매 실행의 첫 단계로 수행한다. Web snapshot 검사는 실제 Web UI 실행이 아니다.
`-SkipBuild`는 source/binary 일치를 입증하지 않으므로 최종 증적은 고정 source에서 빌드한 결과로 만든다.

loopback의 `refinvest_integration`만 허용하며 실행별 `tnorm_<12hex>` schema에 기존 Core migration source·명시 authority candidate SQL을 적용한다.
기존 schema·Flyway history/baseline·서비스 `.env`는 수정하지 않는다. TLS CA·서버 키·RS256 위임·서로 다른 서비스 자격·browser HS256 secret은 실행 폴더에만 둔다.
여섯 legacy TR worker의 Bean 부재를 실제 Main에서 확인한다. Core/Go의 운영 writer 인계를 완료했다는 의미는 아니다.
소유 process와 loopback TLS 중계기를 먼저 종료한 뒤 자신이 만든 schema만 제거한다.

## 경로와 장애 출처

```text
synthetic browser HTTP
  → ordinary Core Main (기존 JWT/CSRF, 목록 issuer, bounded client/decoder)
  → loopback TLS read relay
  → Go Main (caller/profile, authority binding, Guard/Reader/PostgreSQL)
  → loopback TLS authority observer → Core Main primary authority
```

TLS read relay의 목록 경로는 명시 opt-in이며 Core가 재구성한 canonical page/size만 전달한다. authority 경로를 목록 모드로 바꾸지 않는다.
정상 응답과 오류는 실제 Go에서 가져온다. 정상 Go200 이후에만 one-shot body/decoder fault를 적용한다.
decoder probe의 Long.MAX_VALUE total은 합성한 응답 경계 검사이며 실제 DB에 해당 수의 row가 있다는 증거가 아니다.
ID·거래 payload·원 JWT/subject/jti/hash를 evidence에 넣지 않는다. token 하나를 private memory에 보관하며 끝나면 지운다.

목록 fault는2MiB, 기존 metadata fault는16KiB 한계를 유지한다. body cap+1 probe·부분 본문·truncation은 실제 peer 취소와 slot 재사용을 관측한다.
정상 종료는 body 완료 뒤 client/connector/pool 순서, 강제 종료는 transport 취소와 `shutdown incomplete`를 구분한다.
browser abort는 제한된 요청 정리를 검사하며 durable job 취소나 즉시 철회를 의미하지 않는다.

## AL-A 연결과 한계

아래는 시나리오 지도이며 실행 결과는 별도 고정 증적을 따른다. Context의 미실행 표를 이 harness에서 정본 대신 수정하지 않는다.

| 항목 | 이 모드에서 검사하는 경로 | 남는 범위 |
|---|---|---|
| AL-A01~03 | 실제 Kotlin 목록/단건 token→Go 교차 거절, malformed profile/actor·caller read 권한 비상속, 세 Main 선택 조합 | 모든 mutation·미이관 Book 목록/쓰기의 실제 endpoint·운영 fleet |
| AL-A04 | primary Member/caller/key/jti 차단 commit·해제, authority ready=false·기술 readiness 분리 | receipt 불일치/만료의 전체 장애 matrix·모든 SQL 미호출 계측 |
| AL-A05 | public/internal 기본값·경계·중복/alias/unknown/비정규 숫자·최대 offset | 모든 malformed raw wire/ingress 상한 조합 |
| AL-A06~07 | ACTIVE/ARCHIVED·foreign/ADMIN·삭제 상태/tombstone·Book-only, 안정 동률 정렬·범위 밖 total | 실제 raw 삭제 workflow·전체 writer/동시 삭제의 서비스 경합 |
| AL-A08~09 | 실제100개 페이지·큰 string ID·16KiB 초과 성공, 합성 total int64/overflow·구조/정렬·2MiB 선언/stream cap+1 | 실제 Web lossless consumer·전체 heap/OS/성능 SLO |
| AL-A10~11 | body deadline·abort·정상/강제 drain·peer close/slot·오류 registry/no-store/trace·HS256/CSRF·단건 회귀·fallback 부재 | 실제 ingress/fleet·전체 public wire·부하/장시간 실패 |
| AL-A12 | 계약 sync 뒤 일반 Main/실제 authority/DB·독립 flag·readiness 차이 | 실제 Web UI·운영 key/회원/DB history·Core TR writer 중단/복구 |

가시성/정렬과 기존 Go PostgreSQL component 동시성 증거를 실제 서비스의 모든 삭제 경합 성공으로 합산하지 않는다.
Core 선택 client별 admission과 Go 공통 Gate의 범위도 구분한다. 유한 요청 수·크기·시간은 운영 처리량/heap/연결 상한 수용이 아니다.

## 검사와 증적

`python -B -m unittest discover -s scripts -p 'test_owner_query*.py'`는 경로·fault 출처/상한·정수 경계를 검사한다.
기존 contract-sync CI가 같은 pattern으로 실행하며 서비스 E2E 자체가 CI에서 자동 실행된다는 뜻은 아니다.
`evidence.json`은 source revision·source/binary SHA·시나리오 결과·startup 시도·정리 여부만 보관한다.
실패 실행은 성공으로 계산하지 않는다. Windows10014의 제한된 startup 재시도도 기록한다.
최종 payload-free 증적과 실제 수행/미수행 범위는 고정 실행 후 연결한다.
