# 매크로 전략 브리핑 파이프라인 — 영역별 소유 규칙

브리핑 `reports/macro-strategy-briefing.html`은 **여러 소스를 조립한 결과물**이다.
각 영역은 소유자가 다르며, **자기 영역만** 갱신한다. 그래서 미국 이슈를 고쳐도
한국 데이터·스탠스가 지워지지 않고, 스탠스를 갱신해도 한국 이슈 분석이 사라지지 않는다.

```
data/us_issues.json          ─┐
data/kr_issues.json           │
data/stance.json              │  (편집성 분석·판단 — 마켓 브리프 세션 소유)
data/triggers.json            │
                               ├─▶  build_briefing.py  ─▶  reports/macro-strategy-briefing.html  ─▶  아티팩트
data/krx_snapshot_latest.json ┘  (숫자 — GitHub Action 소유, 하루 1회 자동 · 미국장 마감 후)
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
| 트리거 발동/임박 판단 | `data/triggers.json` | `KR` | 마켓 브리프 세션 | JSON 편집 |
| 코스피·코스닥 수급(직전 영업일·MTD·YTD) | `data/krx_snapshot_latest.json` | `KRX` | GitHub Action | 자동(하루 1회) |
| 업종별 등락률 | 〃 | `KRX` | 〃 | 자동 |
| 외국인·연기금 순매수 상위 | 〃 | `KRX` | 〃 | 자동 |
| 그 외 편집성 분석 전부(진단·시나리오 등) | HTML 직접(주석 밖) | — | 마켓 브리프 세션 | HTML 편집 |

## 마켓 브리프 세션이 지켜야 할 3가지

1. **US/KR 소스는 각자 JSON만 고친다.** 미국 이슈는 `us_issues.json`, 한국 이슈 분석은
   `kr_issues.json`, 오늘의 스탠스는 `stance.json`, 트리거 상태는 `triggers.json`. HTML의
   해당 구간을 직접 손대지 말 것 — 빌드가 그 JSON으로 다시 채운다.
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
GitHub Action(`.github/workflows/krx_snapshot.yml`)이 하루 1회(KST 06:20, 미국장 마감 후)
KRX 수집 직후 자동 실행한다. 이 시점을 고른 이유: 이때는 "직전 영업일"(한국 어제자)
데이터가 마감 후 14시간 이상 지나 있어 Open API(T+1)·pykrx 모두 이미 따라잡은 상태라,
두 소스가 서로 다른 날짜를 가리키는 문제가 생기지 않는다.
세션이 US/KR JSON을 갱신한 뒤 즉시 반영하려면, main을 pull → 이 명령 실행 →
`reports/macro-strategy-briefing.html`을 아티팩트로 재배포.

## 데이터 없을 때

소스 파일이 없거나 값이 비면 빌드는 **해당 구간을 건드리지 않고 기존 표기를 유지**한다.
숫자·분석을 지어내지 않고, 있던 내용을 지우지도 않는다.

## triggers.json 스키마

```json
{
  "triggers": [
    {"category": "buy", "tag": "Add", "cond": "KOSPI 7,000~7,200 + 외국인 순매수 전환",
     "act": "한국 비중 20% → 28% 증액", "status": "approaching", "strategies": ["A"]}
  ]
}
```
- `category`: `buy`(추가매수성)·`sell`(축소/손절성)·`watch`(관망/모니터링) — 카드 좌측 색상바.
  액션의 종류를 뜻할 뿐 지금 상태와는 무관 — De-risk 트리거는 평시에도 항상 `sell` 색이다.
- `status`: `dormant`(평시)·`approaching`(임박)·`hit`(발동) — **카드 정렬 순서**(발동 → 임박 → 평시
  순으로 위로 올라옴)와 **tag 옆 강조 배지**(임박/발동)를 결정한다. 조건·액션이 바뀌면(예: 만료된
  이벤트 트리거를 새 이벤트로 교체) 배열 자체를 수정.
- `strategies`: 이 트리거가 적용되는 전략 배열(예: `["A"]`, `["A","B"]`) — cond 앞에 배지로
  표시된다. **전략 C는 원칙적으로 어떤 트리거에도 들어가지 않는다** — C 자체가 "매크로 무반응
  원칙"으로 이 트리거 탭 전체를 무시하도록 설계돼 있기 때문이다. 초기 9개는 각 트리거의 구체적
  수치(20%→28%, 현금 30%, 인덱스풋 등)를 전략 A의 자산배분표와 대조해 A로, 하이퍼스케일러
  CAPEX·하이닉스 ADR FX 관련 3개는 전략 B의 AI메모리 비중과도 겹쳐 A+B로 시드했다 — 이 배정
  자체는 편집성 판단이라 마켓 브리프 세션이 검토·수정해야 한다.
- **status 판단은 편집성 판단이라 자동 계산하지 않는다.** 단, "KOSPI 7,000~7,200 + 외국인
  순매수 전환" 트리거만은 이 파이프라인이 이미 수집하는 두 수치(코스피 종가, 외국인 전일
  순매수)로 검증 가능해서, `fetch_krx_snapshot.py` 실행 로그에 `🔔 KOSPI 매수 트리거 힌트`로
  참고 수치가 매번 출력된다 — 이 힌트를 보고 `status`를 갱신할지는 여전히 사람이 판단한다
  (예: "1~2일 지속 확인" 같은 조건은 하루치 스냅샷만으로 판정할 수 없음). 나머지 8개 트리거는
  미국 CPI·유가·연준·VIX·하이퍼스케일러 실적 등 이 파이프라인이 다루지 않는 데이터라 힌트가 없다.

## 운영 노트 — 마켓 브리프 세션 실전 이슈 (2026-07-10 실측)

1. **push 거부**: `git push`가 "access denied by the git proxy: repo not in this session's
   authorized repository set"로 거부되면 코드 문제가 아니다 — claude.ai 세션 환경설정의 승인
   저장소(sources) 목록에 이 repo(`daybridge2025-star/frank-news-dashboard`)가 없는 것.
   사용자가 환경설정에서 추가해야 하며, 재시도 반복으로는 못 뚫는다. (발생·해결 확인됨.)
2. ~~컨테이너 시계 오차~~ **정정(7/11)**: "세션 시계가 수 시간 빠르다"는 진단은 **검토자(Claude Code
   세션)의 시간 계산 착오였고 철회한다** — 문제의 커밋들(05:50/07:28 KST 7/11)은 정상 시각이었고,
   세션의 "미국장 마감 에디션" 판단도 옳았다. 다만 일반 원칙으로는 유효: 에디션·날짜 라벨은
   컨테이너 시계 단독이 아니라 `data/krx_snapshot_latest.json`의 `bas_dd`·`fetched_at`과
   교차 확인하면 더 안전하다.
3. ~~HTML 커밋 명의~~ **정정(7/11)**: "github-actions[bot] 명의 도용" 의심도 철회 — 해당 커밋은
   진짜 정기 Action이었다(GitHub cron은 예약보다 수십 분 늦게 도는 게 정상: 21:20 예약 → 22:28 실행).
   세션이 HTML까지 직접 커밋하는 것 자체는 허용(렌더러가 멱등이라 Action과 충돌 없음).
4. **Action↔세션 실행 순서**: 정기 Action(현재 21:20 UTC 예약, 실제 ~22:2x 실행)이 세션 에디션
   (~05:50 KST = 20:50 UTC)보다 **늦게** 돌 수 있다. 이 경우 세션이 본 KRX 스냅샷은 아직 갱신 전
   상태다 — 세션은 스냅샷의 `bas_dd`가 기대한 직전 영업일인지 확인하고, 낡았으면 그 수치 인용을
   보류하거나 `gh workflow run krx_snapshot.yml`로 수집을 먼저 트리거할 것.
