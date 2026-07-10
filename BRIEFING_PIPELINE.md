# 매크로 전략 브리핑 파이프라인 — 영역별 소유 규칙

브리핑 `reports/macro-strategy-briefing.html`은 **여러 소스를 조립한 결과물**이다.
각 영역은 소유자가 다르며, **자기 영역만** 갱신한다. 그래서 미국 이슈를 고쳐도
한국 데이터가 지워지지 않고, 한국 데이터를 갱신해도 미국 이슈가 사라지지 않는다.

```
data/us_issues.json         ─┐   (미국 이슈 — 마켓 브리프 세션 소유)
data/krx_snapshot_latest.json┤─▶  build_briefing.py  ─▶  reports/macro-strategy-briefing.html  ─▶  아티팩트
                             ┘   (한국 데이터 — GitHub Action 소유)
```

HTML 안의 `<!--US-START:키-->…<!--US-END:키-->`(미국)와 `<!--KRX-START:키-->…<!--KRX-END:키-->`(한국)
주석 사이 구간만 각 소스로 교체된다. **주석 밖의 손으로 쓴 분석(진단·시나리오·전략·트리거 등)은
빌드가 절대 건드리지 않는다.**

## 소유권

| 영역 | 소스 파일 | 소유자 | 갱신 방법 |
|---|---|---|---|
| 전일 주요 이슈(미국·글로벌) | `data/us_issues.json` | 마켓 브리프 세션 | JSON 편집 |
| 코스피·코스닥 수급 전일 | `data/krx_snapshot_latest.json` | GitHub Action | 자동(하루 2회) |
| 업종별 등락률 | 〃 | 〃 | 자동 |
| 외국인·연기금 순매수 상위 | 〃 | 〃 | 자동 |
| 그 외 편집성 분석 전부 | HTML 직접(주석 밖) | 마켓 브리프 세션 | HTML 편집 |

## 마켓 브리프 세션이 지켜야 할 3가지

1. **미국 이슈는 `data/us_issues.json`만 고친다.** HTML의 `전일 주요 이슈` 카드를 직접
   손대지 말 것 — 빌드가 이 JSON으로 그 구간을 다시 채운다.
2. **HTML의 `KRX-*` / `US-*` 주석 마커를 절대 지우지 말 것.** 마커가 없으면 그 구간은
   자동 주입에서 제외된다(빌드가 경고만 남기고 건너뜀).
3. **아티팩트는 항상 같은 URL로 재배포한다** (기존 북마크 유지).

## us_issues.json 스키마

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

## 빌드 실행

```bash
python build_briefing.py   # data/*.json 을 읽어 HTML의 마커 구간을 채운다(멱등)
```
GitHub Action(`.github/workflows/krx_snapshot.yml`)이 하루 2회 KRX 수집 직후 자동 실행한다.
세션이 us_issues.json을 갱신한 뒤 즉시 반영하려면, main을 pull → 이 명령 실행 →
`reports/macro-strategy-briefing.html`을 아티팩트로 재배포.

## 데이터 없을 때

소스 파일이 없거나 값이 비면 빌드는 **해당 구간을 건드리지 않고 기존 표기를 유지**한다.
숫자를 지어내지 않고, 있던 내용을 지우지도 않는다.
