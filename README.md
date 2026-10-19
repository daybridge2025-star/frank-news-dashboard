# frank-news-dashboard

**매크로 전략 브리핑** — 한국어 투자 브리핑 페이지(`reports/macro-strategy-briefing.html`, 아티팩트로 배포)를 만드는 저장소.

- 구조·영역별 소유권·**본문 작성 규칙**·**문체·형식 가이드**(해요체, JM님 호칭, 기사형 카드)는 [`BRIEFING_PIPELINE.md`](BRIEFING_PIPELINE.md)가 살아있는 스펙이다. 카드·스탠스·트리거·일정 박스를 그리는 코드는 `utils/article.py`.
- 숫자(한국 시세·수급, 미국 시세, 경제 캘린더, 포트폴리오 종가)는 GitHub Actions(`krx_snapshot.yml`)가 매일 자동 수집하고,
  분석(이슈·스탠스·트리거)은 하루 2회 편집 세션이 `data/*.json`에 기록한다. `python build_briefing.py`가 둘을 HTML의 마커 구간에 조립한다.
- 날짜별 작업 기록은 `docs/changelog/`(옵시디언 형식, `_작업일지 인덱스.md`)에 있다 — 페이지에는 일별 경과를 쌓지 않는다.

## 레거시
초기에 이 저장소는 미국 종목 뉴스 수집 대시보드(`collect_news.py`·`daily_briefing.py`·`app.py`·`midnight_reset.py`)였다.
해당 워크플로(`collect_news.yml` 2시간마다 · `daily_briefing.yml` · `midnight_reset.yml`)는 아직 스케줄로 돌고 있으나 브리핑과는 무관하다.
