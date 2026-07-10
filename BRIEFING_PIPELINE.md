# 매크로 전략 브리핑 파이프라인 — 영역별 소유 규칙

브리핑 `reports/macro-strategy-briefing.html`은 **여러 소스를 조립한 결과물**이다.
각 영역은 소유자가 다르며, **자기 영역만** 갱신한다. 그래서 미국 이슈를 고쳐도
한국 데이터·스탠스가 지워지지 않고, 스탠스를 갱신해도 한국 이슈 분석이 사라지지 않는다.

```
data/us_issues.json          ─┐
data/kr_issues.json           │  (편집성 분석 — 마켓 브리프 세션 소유)
data/stance.json              │
                               ├─▶  build_briefing.py  ─▶  reports/macro-strategy-briefing.html  ─▶  아티팩트
data/krx_snapshot_latest.json ┘  (숫자 — GitHub Action 소유, 하루 2회 자동)
```

HTML 안의 `<!--접두사-START:키-->…<!--접두사-END:키-->` 주석 사이 구간만 각 소스로 교체된다.
접두사는 3가지: `KRX`(한국 숫자, Action) · `US`(미국 이슈 분석) · `KR`(한국 이슈 분석·스탠스).
**주석 밖의 손으로 쓴 분석(진단·시나리오·전략·트리거 등)은 빌드가 절대 건드리지 않는다.**

## 소유권

| 영역 | 소스 파일 | 마커 접두사 | 소유자 | 갱신 방법 |
|---|---|---|---|---|
| 전일 주요 이슈(미국·글로벌) | `data/us_issues.json` | `US` | 마켓 브리프 세션 | JSON 편집 |
| 한국 시장 이슈(KRX 수급 해석) | `data/kr_issues.json` | `KR` | 마켓 브리프 세션 | JSON 편집 |
| 오늘의 스탠스 A/B/C | `data/stance.json` | `KR` | 마켓 브리프 세션 | JSON 편집 |
| 코스피·코스닥 수급 전일 | `data/krx_snapshot_latest.json` | `KRX` | GitHub Action | 자동(하루 2회) |
| 업종별 등락률 | 〃 | `KRX` | 〃 | 자동 |
| 외국인·연기금 순매수 상위 | 〃 | `KRX` | 〃 | 자동 |
| 트리거 발동/임박 판단 | — (아직 없음) | — | — | **미구현 — 지금은 HTML 직접 편집** |
| 그 외 편집성 분석 전부(진단·시나리오 등) | HTML 직접(주석 밖) | — | 마켓 브리프 세션 | HTML 편집 |

## 마켓 브리프 세션이 지켜야 할 3가지

1. **US/KR 소스는 각자 JSON만 고친다.** 미국 이슈는 `us_issues.json`, 한국 이슈 분석은
   `kr_issues.json`, 오늘의 스탠스는 `stance.json`. HTML의 해당 구간을 직접 손대지 말 것 —
   빌드가 그 JSON으로 다시 채운다.
2. **HTML의 `KRX-*` / `US-*` / `KR-*` 주석 마커를 절대 지우지 말 것.** 마커가 없으면 그
   구간은 자동 주입에서 제외된다(빌드가 경고만 남기고 건너뜀).
3. **아티팩트는 항상 같은 URL로 재배포한다** (기존 북마크 유지).

## us_issues.json / kr_issues.json 스키마 (동일)

```json
{
  "asof": "미국 7/9 마감 기준",
  "issues": [
    {
      "title": "이슈 제목",
      "desc": ["문단1", "문단2"],
      "impacts": [
        {"strategy": "A 바벨", "level": "g", "text": "전략 A에의 영향"},
        {"strategy": "B 모멘텀", "level": "c", "text": "전략 B에의 영향"}
      ]
    }
  ]
}
```
- `level`: `g`(우호/긍정) · `w`(중립/주의) · `c`(부정/경고) · `s`(심각). 카드 좌측 점 색.
- 텍스트는 평문으로 쓴다(예: `S&P 500`). 빌드가 HTML 이스케이프를 알아서 한다.
- `kr_issues.json`은 `asof` 필드가 필요 없다 — 헤더가 KRX 스냅샷의 기준일을 그대로 재사용한다
  (같은 날짜의 KRX 숫자를 해석하는 내용이므로, 날짜가 따로 놀 위험을 없앴다).
- 처음엔 `issues: []`로 비어 있다 — 채우기 전까지 HTML에 심어둔 "작성 대기" 플레이스홀더가 유지된다.

## stance.json 스키마

```json
{
  "strategies": [
    {"label": "A 바벨", "headline": "한 줄 요약", "detail": "왜 그런지 설명"},
    {"label": "B 모멘텀", "headline": "...", "detail": "..."},
    {"label": "C 초장기", "headline": "...", "detail": "..."}
  ]
}
```
배열 순서 그대로 렌더링된다. 보통 3개(A/B/C)를 유지.

## 빌드 실행

```bash
python build_briefing.py   # data/*.json 을 읽어 HTML의 마커 구간을 채운다(멱등)
```
GitHub Action(`.github/workflows/krx_snapshot.yml`)이 하루 2회 KRX 수집 직후 자동 실행한다.
세션이 US/KR JSON을 갱신한 뒤 즉시 반영하려면, main을 pull → 이 명령 실행 →
`reports/macro-strategy-briefing.html`을 아티팩트로 재배포.

## 데이터 없을 때

소스 파일이 없거나 값이 비면 빌드는 **해당 구간을 건드리지 않고 기존 표기를 유지**한다.
숫자·분석을 지어내지 않고, 있던 내용을 지우지도 않는다.

## 아직 안 된 것 — 트리거 발동/임박 판단

트리거 탭의 9개 `.trg` 카드는 여전히 HTML 직접 편집 대상이다. 이건 "새 글쓰기"가 아니라
"기존 9개 조건 중 뭐가 지금 히트/임박인지 상태만 바꾸는" 문제라 스키마가 다르게 생겨야 한다
(예: 조건 자체는 고정 목록으로 두고 트리거별 `status: dormant|approaching|hit`만 JSON으로
갱신 → HTML은 그 status로 칩 색·정렬만 바꾸는 식). 다음에 필요해지면 별도로 설계한다.
