# Binance USDⓈ-M reconstruction golden dataset v1

이 디렉터리는 Binance USDⓈ-M Linear Perpetual One-way Trade History와 Position History의
정규화, flat-to-flat reconstruction, reconciliation 구현을 검증하기 위한 비운영 Golden Dataset이다.
integration은 정답 fixture와 정적 검증만 소유하며 reconstruction business logic은 포함하지 않는다.

## Contract source

정답 의미는 sibling `context` 저장소의 [ADR-055~059](../../../../context/docs/DECISIONS.md),
[Trading Review domain](../../../../context/docs/TRADING_REVIEW.md),
[reconstruction specification](../../../../context/docs/TRADING_RECONSTRUCTION.md),
[TR-I01~TR-I13](../../../../context/docs/DOMAIN.md),
[glossary](../../../../context/docs/GLOSSARY.md), [roadmap](../../../../context/docs/ROADMAP.md),
[acceptance contract](../../../../context/docs/TRADING_REVIEW_ACCEPTANCE.md)을 따른다.
정책 식별자는 `BINANCE_USDS_LINEAR_PERPETUAL_ONE_WAY_V1`이며 `fixture-index.json`과 모든 manifest에
고정한다. 계약 의미가 바뀌면 기존 expected result를 조용히 덮어쓰지 않고 fixture-set major version을
올리거나 새 디렉터리를 추가한다.

## Layout and authored results

`fixture-index.json`은 30개 fixture, 25개 context scenario mapping과 각 파일의 SHA-256/byte size를
고정한다. 각 fixture에는
`trade-history.csv`, `position-history.csv`, `fixture-manifest.json`과 다음 expected 파일이 있다.

- `canonical-records.json`: Execution/Fee/VenueReportedPnl/PositionSummary record 및 source row provenance
- `reconciliation-manifest.json`: episode, allocation, summary match, capability, exclusion, 보존 결과와 canonical hash
- `quality-report.json`: source/parsed/canonical/episode count, duplicate/conflict, warning, capability 및 version
- `failure.json`: rejected import의 safe failure, retry, LedgerRevision 생성 여부와 masked evidence

`failure.json`은 `REJECTED_IMPORT`에만 필수다. accepted fixture에는 만들지 않는다. 모든 manifest는
context scenario/acceptance scenario, category, canonical/reconstruction/reconciliation/terms version과
`physicalDialectValidation=SYNTHETIC_ONLY`를 명시한다.

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
| `ACCEPTED_AND_ELIGIBLE` | `simple-long`, `simple-short`, `partial-entry`, `partial-exit`, `increase-and-reduce`, `loss-context-increase`, `long-to-short-reversal`, `short-to-long-reversal`, `same-minute-unique-match`, `fee-rebate`, `within-rounding-tolerance`, `duplicate-execution-same-content` |
| `ACCEPTED_BUT_EXCLUDED` | `left-censored`, `right-censored`, `same-timestamp-multiple-trades`, `incomplete-trades`, `trade-episode-only`, `position-summary-only`, `ambiguous-position-match`, `position-pnl-mismatch`, `above-rounding-tolerance`, `different-fee-asset`, `unsupported-position-status` |
| `REJECTED_IMPORT` | `invalid-trade-id`, `invalid-numeric-value`, `execution-identity-conflict`, `hedge-overlap`, `unsupported-coin-m`, `required-column-missing`, `unsupported-source-schema` |

`duplicate-execution-same-content`는 같은 identity/content를 한 canonical record와 복수 provenance로
deduplicate하는 accepted 경로다. Hedge와 COIN-M은 지원 구현이 아니라 명시적 import rejection만 검증한다.

## Context scenario traceability

정본 O.2의 번호를 의미 변경 없이 fixture stable ID에 연결한다. `fixture-index.json`이 기계 판독 정본이며
아래 표는 사람이 검토하기 위한 동일 정보다. 합성 계산 scenario는 모두 `IMPLEMENTED`다.

| Context Scenario | Fixture ID | Category | Expected Outcome |
|---|---|---|---|
| `TR-RECON-SCENARIO-01` | `simple-long` | Accepted + eligible | `COMPLETE / EXACT` |
| `TR-RECON-SCENARIO-02` | `simple-short` | Accepted + eligible | `COMPLETE / EXACT` |
| `TR-RECON-SCENARIO-03` | `partial-entry` | Accepted + eligible | `OPEN / INCREASE / CLOSE` |
| `TR-RECON-SCENARIO-04` | `partial-exit` | Accepted + eligible | `OPEN / REDUCE / CLOSE` |
| `TR-RECON-SCENARIO-05` | `increase-and-reduce` | Accepted + eligible | `COMPLETE / EXACT` |
| `TR-RECON-SCENARIO-06` | `loss-context-increase` | Accepted + eligible | `COMPLETE / EXACT` |
| `TR-RECON-SCENARIO-07` | `long-to-short-reversal` | Accepted + eligible | two disjoint reversal episodes |
| `TR-RECON-SCENARIO-08` | `short-to-long-reversal` | Accepted + eligible | two disjoint reversal episodes |
| `TR-RECON-SCENARIO-09` | `left-censored` | Accepted + excluded | `LEFT_CENSORED / INCOMPLETE_TRADES` |
| `TR-RECON-SCENARIO-10` | `right-censored` | Accepted + excluded | `RIGHT_CENSORED / INCOMPLETE_TRADES` |
| `TR-RECON-SCENARIO-11` | `same-timestamp-multiple-trades` | Accepted + excluded | numeric Trade ID order, right-censored |
| `TR-RECON-SCENARIO-12` | `invalid-trade-id` | Rejected | `FILE_FORMAT_INVALID` |
| `TR-RECON-SCENARIO-13` | `incomplete-trades` | Accepted + excluded | `INCONSISTENT / INCOMPLETE_TRADES` |
| `TR-RECON-SCENARIO-14` | `trade-episode-only` | Accepted + excluded | `POSITION_SUMMARY_MISSING` |
| `TR-RECON-SCENARIO-15` | `position-summary-only` | Accepted + excluded | `POSITION_SUMMARY_WITHOUT_TRADES` |
| `TR-RECON-SCENARIO-16` | `same-minute-unique-match` | Accepted + eligible | secondary equality gives unique match |
| `TR-RECON-SCENARIO-17` | `duplicate-execution-same-content` | Accepted + eligible | deduplicated provenance |
| `TR-RECON-SCENARIO-18` | `execution-identity-conflict` | Rejected | `RECORD_CONFLICT` |
| `TR-RECON-SCENARIO-19` | `fee-rebate` | Accepted + eligible | signed rebate preserved |
| `TR-RECON-SCENARIO-20` | `different-fee-asset` | Accepted + excluded | net outcome capability unavailable |
| `TR-RECON-SCENARIO-21` | `hedge-overlap` | Rejected | `UNSUPPORTED_POSITION_MODE` |
| `TR-RECON-SCENARIO-22` | `unsupported-position-status` | Accepted + excluded | `UNSUPPORTED_POSITION_STATUS` |
| `TR-RECON-SCENARIO-23` | `position-pnl-mismatch` | Accepted + excluded | `PNL_MISMATCH` |
| `TR-RECON-SCENARIO-24` | `within-rounding-tolerance` | Accepted + eligible | `WITHIN_ROUNDING_TOLERANCE` |
| `TR-RECON-SCENARIO-25` | `above-rounding-tolerance` | Accepted + excluded | `SUMMARY_MISMATCH / PNL_MISMATCH` |

Scenario 16 fixture는 같은 instrument의 One-way signed state가 겹치지 않도록 두 flat-to-flat episode를 한
분 안에서 순차 배치한다. 두 Position summary의 primary constraint는 여전히 2×2이고 entry/close price
secondary equality가 유일한 matching을 만든다. 즉 정본의 candidate-graph 의미를 유지하면서
`TR-I07` 수량 보존과 flat boundary도 함께 만족한다.

`PENDING_RAW_SAMPLE`과 `NOT_APPLICABLE_WITH_REASON`은 index validation metadata이고 제품 API enum이 아니다.
현재 25개 계산 scenario에는 사용하지 않는다. 실제 Binance byte dialect 검증만 아래처럼 별도 보류한다.

## Physical dialect validation

CSV는 UTF-8(no BOM), comma delimiter, RFC 4180 quoting, LF line ending과 final newline을 사용하는 합성
serialization이다. 이는 fixture 자체의 결정론적 byte contract일 뿐 실제 Binance Position History의 delimiter,
encoding, BOM 또는 line ending을 검증했다는 뜻이 아니다. 실제 raw sample 확인 상태는
`PENDING_RAW_SAMPLE`이며, 확인 전 parser 계약을 넓히거나 완료로 표시하지 않는다.

## Adding or changing a fixture

1. context 계약에 scenario와 기대 의미가 먼저 존재하는지 확인한다.
2. 전용 synthetic ID 범위에서 두 CSV를 작성하고 required header를 유지한다.
3. 계산식에 따라 expected 값을 수작업으로 작성하고, signed quantity intermediate path와
   source row↔record↔allocation을 직접 연결한다. SUT/parser output을 golden으로 승인하지 않는다.
4. 첫 번째 검산자는 quantity, fee asset별 source amount, reported PnL 보존과 reversal split을 원본 행
   순서대로 확인한다.
5. 두 번째 검산자는 반대 방향으로 average price, gross/fee/net, episode boundary, candidate graph,
   tolerance와 status를 독립 확인한다. 두 검산의 근거가 일치해야 index를 갱신한다.
6. schema와 count를 갱신하고 `fixture-index.json` checksum/size 및 canonical manifest hash를 갱신한다.
7. 아래 validator를 실행한다. 손상 self-test도 기본으로 실행되어 count, reference, privacy namespace
   변조를 거절한다.

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
