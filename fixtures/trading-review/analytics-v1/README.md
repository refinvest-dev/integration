# Trading Review Analytics Golden Dataset V1

이 디렉터리는 Trading Review의 초기 세 Behavior Metric을 구현과 독립적으로 검증하는 비운영 Golden
Dataset이다. Expected Result는 System Under Test가 생성하지 않으며
[`TRADING_ANALYTICS.md`](../../../../context/docs/TRADING_ANALYTICS.md)의 공식과 경계 표를 기준으로 수작업 작성하고
두 번 독립 검산했다. 이 저장소에는 Analytics 계산 로직이 없다.

## Contract source

- [Trading Analytics](../../../../context/docs/TRADING_ANALYTICS.md)
- [Trading Reconstruction](../../../../context/docs/TRADING_RECONSTRUCTION.md)
- [Trading Review](../../../../context/docs/TRADING_REVIEW.md)
- [Domain invariants](../../../../context/docs/DOMAIN.md)
- [ADR-055~060](../../../../context/docs/DECISIONS.md)

Definition bundle은 `TRADING_BEHAVIOR_METRICS_V1`, 세 definition version은 모두 `1`, reconstruction policy는
`BINANCE_USDS_LINEAR_PERPETUAL_ONE_WAY_V1`로 고정한다. Version이나 계산 의미가 바뀌면 기존 expected file을
덮어쓰지 않고 새 fixture root/version을 만든다.

## Layout and reconstruction link

각 fixture는 `input/episodes.json`, pinned `input/reconciliation-manifest.json`, canonical
`input/analysis-config.json`과 네 expected artifact를 가진다. 독립 실행을 위해 필요한 최소 reconstruction
결과를 복사했으며 `fixture-manifest.json`에 synthetic source reconstruction fixture ID, source manifest hash,
policy version, canonical episode hash를 기록한다. Reconciliation manifest의 allocation과 TradingRecord는
embedded synthetic CSV row catalog까지 연결된다.

```text
BehaviorMetric
→ BehaviorObservation
→ FuturesPositionEpisode
→ PositionAllocation
→ TradingRecord
→ synthetic source CSV row
```

## Implemented catalog

| Fixture | Metric | Contract point |
|---|---|---|
| `reentry-before-boundary` | Entry within window | 29분 59초, rate `1/1` |
| `reentry-exact-boundary` | Entry within window | 정확히 30분 포함 |
| `reentry-after-boundary` | Entry within window | 30분 초과, `AVAILABLE` actual zero |
| `reentry-right-observation-censored` | Entry within window | 우측 관찰 불가, `INSUFFICIENT_SAMPLE` |
| `reentry-missing-net-pnl` | Re-entry + exposure | fee/outcome capability 누락, `MISSING_CAPABILITY` |
| `exposure-increase` | Initial exposure change | `150 / 100 = 3/2`, strict-prior baseline 3건 |
| `exposure-different-settlement` | Initial exposure change | 통화 환산 금지, `INSUFFICIENT_SAMPLE` |
| `holding-duration-available` | Holding duration | win median 15분, loss median 40분, 차이 25분 |
| `holding-duration-win-insufficient` | Holding duration | win `1 < 2`, `INSUFFICIENT_SAMPLE` |
| `holding-duration-missing-capability` | Holding duration | structurally in-scope outcome 누락, `MISSING_CAPABILITY` |

이 PR의 우선 범위 밖 catalog는 same-timestamp/reversal/multiple-entry/cross-symbol, exposure decrease/equal/outlier/
zero baseline, holding even-median/outlier/reversal/zero duration 및 명시적 `NOT_APPLICABLE` fixture다. 정본에 이미
expected boundary가 있으며 후속 fixture는 아래 절차로 추가한다. 서비스 endpoint와 persistence 계약은 아직
없으므로 E2E payload를 추측하지 않는다.

## Manual authoring procedure

1. Population을 먼저 고정하고 subject/comparison/excluded episode ID를 나열한다.
2. Decimal은 문자열, rate/ratio는 약분한 `{numerator, denominator}`, duration은 integer microseconds로 쓴다.
3. 정본 공식으로 첫 검산하고, 다른 순서(원시 scalar에서 aggregate 재구성)로 두 번째 검산한다.
4. 두 검산식을 `manualVerification`에 기록한다. SUT output을 expected file에 복사하지 않는다.
5. Observation, allocation, record, source row의 모든 ID를 연결하고 validator를 실행한다.

새 fixture를 추가할 때는 synthetic namespace를 사용해 필수 파일을 만들고 `fixture-index.json`에 file SHA-256과
size를 추가한다. Validator는 expected metric 값을 계산하지 않고 authored result의 구조, reference, count,
status gate와 checksum만 검증한다. 따라서 결과 자동 재작성 기능은 두지 않는다. 계산 구현의 버그가 expected를
같이 바꾸는 위험을 피하기 위함이다.

## Validation

```powershell
scripts/verify-contract-sync.ps1
scripts/verify-trading-review-fixtures.ps1
scripts/verify-trading-analytics-fixtures.ps1
```

마지막 명령은 손상된 count/reference를 거부하는 tamper self-test를 기본 실행한다.

## Synthetic namespace and data safety

모든 object ID는 `synthetic:analytics-v1:<fixture-id>:...`, instrument는
`SYNTHETIC:USDS_FUTURES:AAAUSDT`/`BBBUSDT`, source row key도 같은 namespace를 사용한다. 실제 UID, symbol,
Trade ID, Order ID, 가격, 수량, 손익, credential 또는 production data를 넣지 않는다. `.env.local`,
`.integration-data/`, `compatibility.local.yaml`은 이 package에 포함하지 않는다.
