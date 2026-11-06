# AGENTS.md — integration local conventions

이 저장소는 RefInvest 서비스를 조립하고 검증하기 위한 integration harness다. 제품 도메인,
ADR, OpenAPI 계약 또는 개별 서비스의 business code를 소유하지 않는다.

## 1. Ownership

- `../context`는 ADR, 도메인 문서, OpenAPI 계약의 정본이다.
- `../api/refinvest`는 Core API와 Core 소유 테이블을 소유한다.
- `../compute-api/refinvest`는 Compute API, DatasetSnapshot, Compute 소유 테이블을 소유한다.
- `../web/web`은 브라우저 동작을 소유하며 Core API만 호출한다.
- 이 저장소는 local process composition, non-production fixture, E2E script, compatibility evidence만 소유한다.

계약 또는 제품 규칙을 바꿔야 하면 여기서 복제하거나 수정하지 말고 먼저 `context`를 변경한다.

## 2. Cross-repository rules

- `scripts/verify-contract-sync.ps1`은 E2E 전 필수 검사다. context의 OpenAPI와 Core/Compute의
  동기화 snapshot이 일치하지 않으면 테스트를 진행하지 않는다. Web을 포함하는 E2E는
  `-IncludeWeb`으로 Web의 Core 계약과 `$ref`용 Compute 계약 snapshot도 검증한다.
- Core 문서 snapshot 변경은 `../api/refinvest/scripts/verify-context-doc-sync.ps1`로
  context 정본과의 일치 여부를 검증한다.
- integration은 Git submodule, runtime library dependency, 또는 구현 레포의 source root가 아니다.
  sibling checkout 경로는 script parameter로 바꿀 수 있어야 한다.
- Core와 Compute의 internal API key는 `REFINVEST_INTERNAL_API_KEY` 하나를 정본으로 사용한다.
  별도의 Core/Compute key를 환경 파일에 중복 정의하지 않는다.
- `.env.local`, `.integration-data/`, `compatibility.local.yaml`은 로컬 전용이며 커밋하지 않는다.

## 3. Fixture and E2E

- fixture는 외부 vendor credential이나 production data에 의존하지 않아야 한다.
- fixture DatasetSnapshot은 immutable ID와 고정된 데이터 범위를 사용한다.
- API smoke는 정상 JWT 검증, CSRF, Core persistence, Core-to-Compute dispatch, terminal result
  persistence를 실제 HTTP/DB 경로로 검증한다.
- 실패한 E2E run을 성공으로 해석하지 않는다. 실패 원인을 로그와 integration database에서 확인한다.

## 4. Local data safety

- integration DB 이름은 production/development DB와 분리한다. 기본값은 `refinvest_integration`이다.
- script는 integration DB 이외의 destructive database operation을 수행하지 않는다.
- local PostgreSQL을 우선 지원하고, Docker Compose는 선택적인 대안이다.

## PR 본문 형식

`.github/pull_request_template.md`를 기준으로 Summary, Changes, Validation, Impact,
Related Links를 항상 포함한다. 제목은 영어 H2(`## Summary`), 본문은 한국어로 작성하고
전문 용어·식별자·명령은 원문을 사용한다. 빈 안내 문구는 실제 내용으로 바꾼다.
해당 사항이 없으면 `해당 없음`과 간단한 이유를 적는다. Validation에는 실제 실행 명령/결과와
미실행 검증을 구분하며, Impact에는 호환성·소유권·운영 영향 또는 영향 없음의 근거를 적는다.
Related Links에는 관련 ADR/Use Case/문서/이슈/상대 저장소 PR을 연결한다.
Migration Plan, Rollback, Evidence, Review Notes 등 추가 항목은 필요한 경우에만 영어 H2로 넣는다.
Migration/배포 변경은 적용·복구 순서, E2E/성능 주장은 실제 증적을 추가한다.
제목은 커밋 컨벤션을 따르고, 본문은 대화 이력 대신 최종 변경을 설명한다.
API/CLI로 PR을 생성·갱신하는 자동화도 동일 형식을 적용한다. 이 문서와 template은 작성 기준이며
CI에서 본문 형식을 자동 강제하는 기능이 이미 구축됐다는 의미는 아니다.
