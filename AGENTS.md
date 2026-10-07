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
