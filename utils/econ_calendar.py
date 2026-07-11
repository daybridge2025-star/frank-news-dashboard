"""
이번 주(월~일) 주요 경제 캘린더 수집 — Finnhub /calendar/economic.

app.py의 fetch_economic_calendar()와 같은 엔드포인트·같은 주간(월~일 KST) 로직을
재사용한다. 이 엔드포인트는 Finnhub 문서상 "Premium" 표시가 있고, app.py에도
이미 403 처리가 존재한다(과거에 실제로 막힌 이력이 있다는 뜻) — 그래서 상태를
'ok'/'403'/'401'/기타로 명확히 구분해 반환한다. 어느 쪽이든 여기서 지어내는
값은 없다: 막히면 빈 리스트 + 상태 코드만 돌려주고, 호출부가 로그로 판단하게 한다.
"""

import os
import requests
from datetime import datetime, timedelta, timezone

_UA = {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36')}
_KST = timezone(timedelta(hours=9))

# "주요" 일정만 남긴다 — 미국·한국 고영향(high) 이벤트로 한정(app.py 사이드바 기본값과 동일).
COUNTRIES = ('US', 'KR')
IMPACT = ('high',)


def week_range(today=None):
    """이번 주 월요일·일요일(KST 기준) 반환."""
    d = today or datetime.now(_KST).date()
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def fetch_week():
    """
    이번 주 주요(미국·한국 고영향) 경제 일정.
    반환: (events: list[dict], status: 'ok'|'no_key'|'401'|'403'|str(예외))
    events는 status가 'ok'일 때만 채워진다 — 그 외엔 항상 빈 리스트(기존 표기 유지용).
    """
    api_key = os.environ.get('FINNHUB_API_KEY', '')
    if not api_key:
        print('[EconCal] FINNHUB_API_KEY 없음 — 경제 캘린더 섹션은 기존 표기 유지')
        return [], 'no_key'

    monday, sunday = week_range()
    try:
        r = requests.get(
            'https://finnhub.io/api/v1/calendar/economic',
            params={'from': monday.isoformat(), 'to': sunday.isoformat(), 'token': api_key},
            headers=_UA, timeout=10)
        if r.status_code == 401:
            print('[EconCal] HTTP 401 — API 키 오류(무효 키)')
            return [], '401'
        if r.status_code == 403:
            print('[EconCal] HTTP 403 — Finnhub 현재 플랜에서 경제캘린더 API 접근 제한'
                  '(Premium 플랜 필요로 확인됨)')
            return [], '403'
        r.raise_for_status()
        raw = r.json().get('economicCalendar') or []
    except Exception as e:
        print(f'[EconCal] 수집 실패: {e}')
        return [], str(e)

    events = [e for e in raw
              if e.get('country') in COUNTRIES
              and (e.get('impact') or '').lower() in IMPACT]
    events.sort(key=lambda x: x.get('time', ''))
    print(f'[EconCal] {monday}~{sunday} 주요 일정 {len(events)}건 '
          f'(전체 {len(raw)}건 중 미국·한국 고영향 필터링) 수집 — HTTP 200')
    return events, 'ok'
