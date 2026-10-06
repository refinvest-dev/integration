# Binance USDⓈ-M raw contract Golden Fixture Set v3

이 디렉터리는 Context PR #29에서 확정한 Binance USDⓈ-M Futures dual-artifact CSV 계약을 검증하는 integration 소유 합성 golden set이다. 정본은 [Trading Review](../../../../context/docs/TRADING_REVIEW.md), [Reconstruction](../../../../context/docs/TRADING_RECONSTRUCTION.md), [Acceptance](../../../../context/docs/TRADING_REVIEW_ACCEPTANCE.md), [Versioning](../../../../context/docs/TRADING_VERSIONING.md), [ADR-058/059](../../../../context/docs/DECISIONS.md), [Core OpenAPI](../../../../context/openapi/core-api.yaml), [Compute OpenAPI](../../../../context/openapi/compute-api.yaml)다.

## Version boundary

`fixtureSetVersion: 3`은 fixture package 버전이다. 제품 계약은 계속 `BINANCE_USDS_TRADE_HISTORY_V1`, `BINANCE_USDS_POSITION_HISTORY_V1`, `BINANCE_USDS_LINEAR_PERPETUAL_ONE_WAY_V1`이다. `FIXTURE_ONLY_*`와 `FIXTURE_METADATA:*` 식별자는 제품 registry 식별자가 아니라 이 package의 serializer/normalizer/terms metadata다.

기존 [`binance-usds-v1`](../binance-usds-v1/README.md)은 과거 minute-shaped synthetic 검증 자산이므로 수정하지 않는다. `fixture-index.json`은 그 tree hash를 고정한다.

## Privacy and provenance

실제 Binance CSV, 실제 UID/Trade ID/Order ID/symbol/가격/수량/PnL, 실제 경로와 hash를 포함하지 않는다. 실제 표본에서는 aggregate 관찰 결과(BOM, comma, CRLF, no quote observed, exact-second timestamp, lowercase boolean, source-order scrambling, same-order fills, Unicode symbol, 독립 coverage, gross Closing PNL)만 가져왔다. 모든 값은 `RAW_V3_SYNTHETIC_Fxx` namespace에서 손으로 확정한 합성값이다. source row number는 header를 제외한 1-based physical data row이며, exact row byte hash를 evidence에 고정한다.

## Physical contract

정상 raw-shaped CSV는 UTF-8 BOM으로 시작하고 comma delimiter, CRLF-only, final CRLF, exact-case required header를 사용한다. Trade boolean은 `true|false`, 세 timestamp field는 `yyyy-MM-dd HH:mm:ss`다. parser 제품 계약은 optional BOM과 LF transport도 수용하지만, 이 golden set은 실제 관찰 형식을 재현하기 위해 더 좁은 bytes를 고정한다. Position timestamp는 minute interval이 아니라 pinned timezone을 적용한 exact UTC instant다.

## Coverage and expected results

Trade coverage(`Time` min/max)와 Position coverage(`Opened` min, `Closed` max)는 독립 `ArtifactCoverage`로 기록한다. 둘 다 `SECOND`, `NOT_PROVEN`이며 synthetic scenario 내부의 의도적 완결성과 거래소 export 완전성을 구분한다. `commonAnalyzableInterval`은 관측 경계의 교집합일 뿐 완전성 주장이 아니다.

Expected 파일은 SUT 출력으로 승인하거나 reconstruction을 실행해 생성하지 않았다. Context O.2의 25개 계산표를 다시 검산해 signed quantity/action, max/closed volume, exact rational entry/exit, realized PnL, asset별 fee, gross/fee/net, provenance/evidence와 eligibility를 고정했다. 도구는 이미 확정한 JSON의 deterministic serialization과 checksum만 계산한다.

## Fixture catalog

| Fixture | Logical source | Context scenario | Contract focus |
|---|---|---|---|
| `simple-long` | `simple-long` | TR-RECON-SCENARIO-01 | 25-scenario rewrite |
| `simple-short` | `simple-short` | TR-RECON-SCENARIO-02 | 25-scenario rewrite |
| `partial-entry` | `partial-entry` | TR-RECON-SCENARIO-03 | 25-scenario rewrite |
| `partial-exit` | `partial-exit` | TR-RECON-SCENARIO-04 | 25-scenario rewrite |
| `increase-and-reduce` | `increase-and-reduce` | TR-RECON-SCENARIO-05 | 25-scenario rewrite |
| `loss-context-increase` | `loss-context-increase` | TR-RECON-SCENARIO-06 | 25-scenario rewrite |
| `long-to-short-reversal` | `long-to-short-reversal` | TR-RECON-SCENARIO-07 | 25-scenario rewrite |
| `short-to-long-reversal` | `short-to-long-reversal` | TR-RECON-SCENARIO-08 | 25-scenario rewrite |
| `left-censored` | `left-censored` | TR-RECON-SCENARIO-09 | 25-scenario rewrite |
| `right-censored` | `right-censored` | TR-RECON-SCENARIO-10 | 25-scenario rewrite |
| `same-timestamp-multiple-trades` | `same-timestamp-multiple-trades` | TR-RECON-SCENARIO-11 | 25-scenario rewrite |
| `invalid-trade-id` | `invalid-trade-id` | TR-RECON-SCENARIO-12 | 25-scenario rewrite |
| `incomplete-trades` | `incomplete-trades` | TR-RECON-SCENARIO-13 | 25-scenario rewrite |
| `trade-episode-only` | `trade-episode-only` | supplemental historical regression | supplemental rewrite |
| `position-summary-only` | `position-summary-only` | TR-RECON-SCENARIO-15 | 25-scenario rewrite |
| `same-minute-unique-match` | `same-minute-unique-match` | supplemental historical regression | supplemental rewrite |
| `duplicate-execution-same-content` | `duplicate-execution-same-content` | TR-RECON-SCENARIO-17 | 25-scenario rewrite |
| `execution-identity-conflict` | `execution-identity-conflict` | TR-RECON-SCENARIO-18 | 25-scenario rewrite |
| `fee-rebate` | `fee-rebate` | TR-RECON-SCENARIO-19 | 25-scenario rewrite |
| `different-fee-asset` | `different-fee-asset` | TR-RECON-SCENARIO-20 | 25-scenario rewrite |
| `hedge-overlap` | `hedge-overlap` | TR-RECON-SCENARIO-21 | 25-scenario rewrite |
| `unsupported-position-status` | `unsupported-position-status` | TR-RECON-SCENARIO-22 | 25-scenario rewrite |
| `position-pnl-mismatch` | `position-pnl-mismatch` | TR-RECON-SCENARIO-23 | 25-scenario rewrite |
| `within-rounding-tolerance` | `within-rounding-tolerance` | TR-RECON-SCENARIO-24 | 25-scenario rewrite |
| `above-rounding-tolerance` | `above-rounding-tolerance` | TR-RECON-SCENARIO-25 | 25-scenario rewrite |
| `ambiguous-position-match` | `ambiguous-position-match` | raw contract extension | 25-scenario rewrite |
| `invalid-numeric-value` | `invalid-numeric-value` | raw contract extension | 25-scenario rewrite |
| `required-column-missing` | `required-column-missing` | raw contract extension | 25-scenario rewrite |
| `unsupported-coin-m` | `unsupported-coin-m` | raw contract extension | 25-scenario rewrite |
| `unsupported-source-schema` | `unsupported-source-schema` | raw contract extension | 25-scenario rewrite |
| `raw-byte-dialect` | `simple-long` | TR-RECON-SCENARIO-01 | raw-byte-dialect |
| `second-precision-position` | `simple-long` | TR-RECON-SCENARIO-01 | second-precision-position |
| `source-order-scrambled` | `partial-entry` | TR-RECON-SCENARIO-03 | source-order-scrambled |
| `unicode-symbol` | `simple-long` | TR-RECON-SCENARIO-01 | unicode-symbol |
| `multiple-fills-same-order` | `partial-entry` | TR-RECON-SCENARIO-03 | multiple-fills-same-order |
| `left-censored-before-trade-coverage` | `left-censored` | TR-RECON-SCENARIO-09 | left-censored-before-trade-coverage |
| `episode-after-position-coverage` | `trade-episode-only` | TR-RECON-SCENARIO-14 / TR-ACC-RECON-012 | episode-after-position-coverage |
| `common-window-exact-reconciliation` | `increase-and-reduce` | TR-RECON-SCENARIO-05 | common-window-exact-reconciliation |
| `closing-pnl-excludes-fee` | `simple-long` | TR-RECON-SCENARIO-01 | closing-pnl-excludes-fee |
| `exact-timestamp-non-unique-match` | `ambiguous-position-match` | TR-RECON-SCENARIO-16 / TR-ACC-RECON-008 | exact-timestamp-non-unique-match |

명시 raw 계약 fixture 10개는 `raw-byte-dialect`, `second-precision-position`, `source-order-scrambled`, `unicode-symbol`, `multiple-fills-same-order`, `left-censored-before-trade-coverage`, `episode-after-position-coverage`, `common-window-exact-reconciliation`, `closing-pnl-excludes-fee`, `exact-timestamp-non-unique-match`다. 이들은 각각 TR-ACC-IMPORT-001, TR-ACC-RECON-001/002/004/008/012 등과 `fixture-manifest.json`에서 연결된다.

## Validation

```powershell
.\scripts\verify-contract-sync.ps1
.\scripts\verify-trading-review-fixtures.ps1 -Dataset v1
.\scripts\verify-trading-review-fixtures.ps1 -Dataset raw-v3
.\scripts\verify-trading-review-fixtures.ps1 -Dataset all
.\scripts\verify-trading-analytics-fixtures.ps1
```

raw-v3 검증은 BOM 제거, CRLF→LF, header case, seconds 제거, Trade ID 중복/비숫자, source row hash, coverage, manifest hash, Unicode normalization, Closing PNL fee 차감, evidence link 제거를 임시 복사본에서 손상시켜 모두 거절되는지도 확인한다.

## Expected change procedure

1. Context 정본과 OpenAPI snapshot을 먼저 변경·병합한다.
2. 실제 raw 값을 보지 않고 새 synthetic namespace로 input과 수작업 계산표를 작성한다.
3. 각 fixture의 `manualVerification` 항목과 source→record→allocation→ReviewUnit→summary chain을 사람이 검산한다.
4. deterministic JSON/byte hash만 다시 계산한다. Compute 출력으로 expected를 덮어쓰지 않는다.
5. v1 tree hash 불변, dataset별 validator, corruption self-test, contract sync를 모두 실행한다.

## Remaining questions

- Cross Margin
- 겹치는 별도 export 간 중복·충돌
- 다른 fee asset과 실제 rebate
- Hedge Mode 실제 export
- 서로 다른 계정 파일의 잘못된 조합
- Binance export format의 장기 버전 변화
