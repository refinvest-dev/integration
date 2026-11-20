# ADR-085 Import Normalization pin hash oracle

이 디렉터리는 [ADR-085](https://github.com/refinvest-dev/context/blob/d289c2c5711da83a2fe38fe09ca0d2e605b4f342/docs/DECISIONS.md#adr-085--import-pin-hash-terms-snapshot-and-approval-provenance)·
[Versioning §2.2](https://github.com/refinvest-dev/context/blob/d289c2c5711da83a2fe38fe09ca0d2e605b4f342/docs/TRADING_VERSIONING.md#22-신규-import-normalization-pin-hash-규칙)의 신규 고정 vector다.
기존 normalization input/result/flat engine vector와 파일은 유지한다.

## 증명하는 범위

`normalization-pin-hash-v1.json`은 6개 synthetic pin의 component 2개와 composite, 합계 18개의 projection/canonical JSON/UTF-8 hex/예상 SHA-256을 고정한다.
ASCII, 보충 문자/private-use key의 UTF-16 정렬, NFC/NFD 값 차이, component 독립 변경과 composite 전파, quote/backslash/slash·HTML 문자·U+2028/U+2029/정상 U+FFFD를 포함한다.
3개 동등 JSON 표현과 3개 hash 관계, 24개 거절 사례를 별도로 둔다. failure 이름은 fixture의 내부 분류이며 새로운 공개 API 오류 code가 아니다.
`normalization-pin-hash-v1.schema.json`은 fixture/input shape를 검증한다. component는 자기 hash만 제외하고 composite는 선언/검증된 child hash를 포함한다.
unknown/missing/null/empty terms·잘못된 hash/자료형·중복 key·잘못된 UTF-8/단독 surrogate·trailing JSON·다른 projection을 거절한다.

## 생성과 독립 검증

```sh
node scripts/generate-trading-import-pin-hash-v1.mjs --write
node scripts/generate-trading-import-pin-hash-v1.mjs --check
python -B scripts/verify-trading-import-pin-hash-v1.py
```

명령은 Context 저장소 root에서 실행한다. 소비자에는 README/vector/schema만 bytes로 전파하며 생성기/검증기 script는 Context가 소유한다.

생성기는 Node crypto·직접 string/object JCS 직렬화로 expected 값을 기록한다. 기본 동작/CI의 `--check`는 파일을 수정하지 않는다.
Python 검증기는 별도 UTF-16 정렬·문자열 처리·hashlib 구현으로 저장된 projection/text/bytes/hash를 검증한다. production hasher가 테스트 중 expected 값을 만드는 방식이 아니다.
두 도구는 ADR-085의 string/object subset만 다루며 일반 JCS number 구현을 주장하지 않는다. 같은 expected/hash가 나오더라도 실행 지원이 승인된 것은 아니다.
외부 fixture 파일의 LF/CRLF 줄바꿈만 검사 읽기에서 통일한다. 내부 canonical string/UTF-8 hex/hash 값은 그대로 정확히 비교하며 파일을 재작성하지 않는다.
기대값 변경은 명시 `--write`와 projection/bytes diff 리뷰를 거쳐야 한다. test 실패를 해결하기 위해 원 fixture를 조용히 재생성하지 않는다.

모든 instrument/version/digest는 비민감 synthetic 값이며 실제 vendor 데이터·terms snapshot·등록 profile·배포물의 digest가 아니다.
두 component digest가 서로 다른 예시는 hash 독립성 검사용이다. 현재 Compute의 단일 engine digest 지원을 통과한다고 주장하지 않는다.
이 vector의 성공은 실제 provider/Compute/DB/HTTP·메모리/운영 인계 수용이 아니다.
