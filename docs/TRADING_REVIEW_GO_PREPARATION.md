# Trading Review Go integration 준비

설계 정본은 context의 [서비스 경계](https://github.com/refinvest-dev/context/blob/main/docs/TRADING_REVIEW_SERVICE_BOUNDARY.md)/ADR-072,
Go 구현 설계는 [ARCHITECTURE.md](https://github.com/refinvest-dev/trading-review-api/blob/main/ARCHITECTURE.md)다.
로컬 checkout 경로는 integration 저장소 루트 기준 `../context`, `../trading-review-api/trading-review-api`다.
이 문서는 integration 후속 작업만 소유한다. 현재 Go 서버/검증 자동화가 구현됐다는 의미가 아니다.

## 조합
- 기존 PostgreSQL을 유지하고 Trading Review 관련 테이블만 Go가 소유한다.
- Core 중단 중에는 harness가 Go를 직접 검증한다.
- 초기 외부 연결은 Core 인증/중계 → Go, 이후 Gateway → Go다.
- Core의 기존 Trading Review Worker와 Go의 동시 쓰기 운영은 요구하지 않는다.
- 테스트는 기존 사용자 데이터 DB가 아닌 integration DB에서 수행한다.

## 필요한 작업
1. tradingReviewApi 경로 parameter와 API/Worker start/stop/status, 종료·재시작 증거 추가.
2. migration baseline과 모듈별 소유권, 서비스 전체 실행 순서 검증.
3. contract-sync-map/contract_sync.py/verify-contract-sync.ps1의 Go 대상·revision 검증 확장.
4. Go 문서 snapshot namespace와 Context golden vector byte 비교 추가.
5. propagation/candidate/evidence workflow 및 compatibility schema에 Go checkout SHA 추가.
6. 기존 fixture의 Core SQL/테이블/프로세스 의존을 검토하고 Go E2E로 연결.
7. 삭제/발행 경합, 교차 rollback, R2/A2 실패, ACK crash, 멱등 접수, 취소 경합 검증.
8. 사용자 위임·소유권·CSRF 경계를 포함한 Core→Go smoke와 기존 Backtest 회귀 유지.

E2E 전에 계약 동기화 검사를 수행한다. 새 context 문서 변경 후 기존 snapshot은 별도 전파가
필요하며 동기화되지 않은 조합을 성공으로 간주하지 않는다. Go 원격 저장소는 연결되어 있지만
Go module 경로와 CI의 실제 후보 revision은 별도로 확정·검증한다.
