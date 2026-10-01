# Binance USDⓈ-M reconstruction golden dataset v1

이 디렉터리는 Binance USDⓈ-M Linear Perpetual One-way Trade History와 Position History의
정규화, flat-to-flat reconstruction, reconciliation 구현을 검증하기 위한 비운영 Golden Dataset이다.
integration은 정답 fixture와 정적 검증만 소유하며 reconstruction business logic은 포함하지 않는다.

## Contract source

정답 의미는 sibling `context` 저장소의 [ADR-055~059](../../../../context/docs/DECISIONS.md),
[Trading Review domain](../../../../context/docs/TRADING_REVIEW.md),
[reconstruction specification](../../../../context/docs/TRADING_RECONSTRUCTION.md),
[TR-I01~TR-I13](../../../../context/docs/DOMAIN.md),
[glossary](../../../../context/docs/GLOSSARY.md), [roadmap](../../../../context/docs/ROADMAP.md)을 따른다.
정책 식별자는 `BINANCE_USDS_LINEAR_PERPETUAL_ONE_WAY_V1`이며 `fixture-index.json`과 모든 manifest에
고정한다. 계약 의미가 바뀌면 기존 expected result를 조용히 덮어쓰지 않고 fixture-set major version을
올리거나 새 디렉터리를 추가한다.

## Layout and authored results

`fixture-index.json`은 21개 fixture와 각 파일의 SHA-256/byte size를 고정한다. 각 fixture에는
`trade-history.csv`, `position-history.csv`, `fixture-manifest.json`과 다음 expected 파일이 있다.

- `canonical-records.json`: Execution/Fee/VenueReportedPnl/PositionSummary record 및 source row provenance
- `reconciliation-manifest.json`: episode, allocation, summary match, capability, exclusion, 보존 결과와 canonical hash
- `quality-report.json`: source/parsed/canonical/episode count, duplicate/conflict, warning, capability 및 version

Decimal은 JSON number가 아니라 `{"unscaled":"...","scale":n}`으로 저장한다. 평균 가격은 exact
numerator/denominator이고 timestamp는 UTC RFC 3339 microsecond 6자리다. 사람 검산값을 정렬해 기록하며
System Under Test가 expected result를 생성하지 않는다. `canonicalManifestHash`는 해당 필드 자체를 제외한
정렬·공백 없는 UTF-8 JSON SHA-256이다. runtime UUID와 생성 시각은 hash 입력에 없다.

## Synthetic namespace and privacy

이 dataset은 실제 Binance export에서 파생되지 않았다. UID는 `990000000000`~`990000009999`, Trade ID는
`910000000000`~`910000009999`, Order ID는 `920000000000`~`920000009999` 범위만 사용한다. 지원 fixture
symbol은 `AAAUSDT`이고 COIN-M 거절 fixture만 `AAAUSD`를 사용한다. 모든 가격·수량·fee·PnL도 정본 scenario를
표현하기 위해 새로 만든 합성값이다.

실제 CSV, UID, account fingerprint, filename, storage reference, credential 또는 production data를 이
디렉터리나 log/PR 본문에 복사하면 안 된다. 실제 파일을 조사해야 할 때도 입력은 ignored
`.integration-data/` 아래에서만 읽고 원시 행을 출력하지 않는다.

## Catalog

| Category | Fixtures |
|---|---|
| Accepted + eligible | `simple-long`, `simple-short`, `partial-entry`, `partial-exit`, `increase-and-reduce`, `long-to-short-reversal`, `same-timestamp-multiple-trades`, `fee-rebate`, `duplicate-execution-same-content` |
| Accepted + excluded | `left-censored`, `right-censored`, `incomplete-trades`, `ambiguous-position-match`, `position-pnl-mismatch`, `different-fee-asset`, `unsupported-position-status` |
| Rejected import | `execution-identity-conflict`, `hedge-overlap`, `unsupported-coin-m`, `required-column-missing`, `unsupported-source-schema` |

`duplicate-execution-same-content`는 같은 identity/content를 한 canonical record와 복수 provenance로
deduplicate하는 accepted 경로다. Hedge와 COIN-M은 지원 구현이 아니라 명시적 import rejection만 검증한다.

## Adding or changing a fixture

1. context 계약에 scenario와 기대 의미가 먼저 존재하는지 확인한다.
2. 전용 synthetic ID 범위에서 두 CSV를 작성하고 required header를 유지한다.
3. 계산식에 따라 expected 값을 수작업으로 작성하고, source row↔record↔allocation을 직접 연결한다.
4. quantity, fee asset별 source amount, reported PnL을 첫 번째 검산한다.
5. 다른 순서로 average price, gross/fee/net, episode boundary와 summary constraint를 두 번째 검산한다.
6. schema와 count를 갱신하고 `fixture-index.json` checksum/size 및 canonical manifest hash를 갱신한다.
7. 아래 validator를 실행한다. 손상 self-test도 기본으로 실행되어 checksum 또는 count 변조를 거절한다.

```powershell
.\scripts\verify-trading-review-fixtures.ps1
```

Cross-service E2E/compatibility를 연결하기 전에는 반드시
`.\scripts\verify-contract-sync.ps1`을 먼저 실행한다. 현재 dataset은 확정되지 않은 Trading Review endpoint나
persistence schema를 추측하지 않으므로 정적 compatibility evidence까지만 제공한다.

## Version policy and deferred analytics

설명이나 새 독립 scenario 추가처럼 기존 의미를 바꾸지 않는 변경은 fixture-set minor version으로 관리할 수
있다. dialect, canonical field 의미, Decimal/hash serialization, reconstruction/reconciliation 정답이 바뀌면
major version 디렉터리를 추가한다. 이미 소비된 expected 파일은 같은 version에서 의미를 바꾸지 않는다.

Analytics Metric, BehaviorObservation, BehaviorFinding은 이 dataset 범위가 아니다. 이 fixture는 import와
reconstruction/reconciliation의 정확성·근거·eligible/excluded population까지만 고정한다. Metric 정의와
comparison population은 별도의 context 계약과 후속 fixture에서 검증한다.
