"""
이번 주(월~일) 주요 경제 캘린더 수집 — FRED 릴리스 캘린더 + 연준 공식 FOMC 일정.

소스 이력: 원래 Finnhub /calendar/economic이었으나 무료 플랜 403이 실측 확정돼
(2026-07-12 cron, BRIEFING_PIPELINE.md 운영 노트 5번) 사용자 결정으로 교체.
  1. FRED /fred/releases/dates (FRED_API_KEY — us_snapshot과 같은 시크릿):
     미국 주요 지표의 발표 예정일. 예상치·발표치는 없고 "언제 나오는지"만 제공.
  2. data/fomc_schedule.json (연준 공식 발표, 연 1회 수동 갱신):
     FOMC 금리 결정일 + 의사록 공개일(결정일 +21일, 연준 관례) — 키 불필요.

원칙 동일: 실패해도 지어내지 않는다. FRED가 막히면 FOMC 일정만 반환하고
fred_ok=False로 표시해 렌더러가 "지표 일정 미확보"를 명시하게 한다.
"""

import json
import os
import requests
from datetime import datetime, timedelta, timezone

_UA = {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36')}
_KST = timezone(timedelta(hours=9))

FOMC_PATH = 'data/fomc_schedule.json'

# FRED 릴리스 큐레이션 — release_name 부분일치(소문자) → (한글 표기, 중요도).
# 여기 없는 릴리스는 표시하지 않는다(수백 개 중 시장 영향 큰 것만).
FRED_RELEASE_MAP = (
    ('consumer price index',                ('CPI(소비자물가지수)', 'high')),
    ('employment situation',                ('고용보고서(비농업 고용·실업률)', 'high')),
    ('gross domestic product',              ('GDP(국내총생산)', 'high')),
    ('personal income and outlays',         ('PCE 물가·개인소득/지출', 'high')),
    ('advance monthly sales',               ('소매판매', 'high')),
    ('producer price index',                ('PPI(생산자물가지수)', 'medium')),
    ('unemployment insurance weekly claims', ('주간 신규 실업수당 청구', 'medium')),
    ('industrial production',               ('산업생산·설비가동률', 'medium')),
    ('new residential construction',        ('주택착공·건축허가', 'medium')),
    ('job openings and labor turnover',     ('JOLTS 구인·이직', 'medium')),
    ('university of michigan',              ('미시간대 소비자심리', 'medium')),
    ('surveys of consumers',                ('미시간대 소비자심리', 'medium')),
)


def week_range(today=None):
    """브리핑이 다룰 주(월~일, KST) 반환.
    주말(토·일)에 실행되면 끝나가는 이번 주가 아니라 다가오는 주를 다룬다 —
    일요일 아침 브리핑의 '이번 주 주요 일정'은 독자에게 다음 주를 뜻하기 때문."""
    d = today or datetime.now(_KST).date()
    if d.weekday() >= 5:  # 토(5)·일(6) → 다음 월요일 기준
        monday = d + timedelta(days=7 - d.weekday())
    else:
        monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _match_release(name):
    low = (name or '').lower()
    for key, val in FRED_RELEASE_MAP:
        if key in low:
            return val
    return None


def fetch_fred_releases(monday, sunday):
    """
    FRED 릴리스 캘린더에서 해당 주의 큐레이션된 발표 일정을 가져온다.
    반환: (events, status) — status 'ok'가 아니면 events는 빈 리스트.
    include_release_dates_with_no_data=true 여야 아직 발표 전인 미래 날짜가 나온다.
    """
    api_key = os.environ.get('FRED_API_KEY', '')
    if not api_key:
        print('[EconCal] FRED_API_KEY 없음 — 지표 발표 일정 미확보(FOMC 일정만 표시)')
        return [], 'fred_no_key'
    try:
        r = requests.get(
            'https://api.stlouisfed.org/fred/releases/dates',
            params={'api_key': api_key, 'file_type': 'json',
                    'realtime_start': monday.isoformat(),
                    'realtime_end': sunday.isoformat(),
                    'include_release_dates_with_no_data': 'true',
                    'limit': 1000, 'sort_order': 'asc'},
            headers=_UA, timeout=15)
        if not r.ok:
            print(f'[EconCal] FRED HTTP {r.status_code}: {r.text[:200]}')
            return [], f'fred_http_{r.status_code}'
        raw = r.json().get('release_dates') or []
    except Exception as e:
        print(f'[EconCal] FRED 수집 실패: {e}')
        return [], f'fred_error: {e}'

    lo, hi = monday.isoformat(), sunday.isoformat()
    # raw를 날짜 오름차순으로 본 뒤 지표명 기준 첫(=가장 이른) 발표일만 남긴다 —
    # 주간 실업수당처럼 한 주에 발표일이 둘로 잡히는 릴리스가 중복 행으로 보이던 것 방지.
    events, seen_names = [], set()
    for it in sorted(raw, key=lambda x: x.get('date') or ''):
        d = it.get('date') or ''
        if not (lo <= d <= hi):          # 파라미터와 무관하게 방어적으로 주간 필터
            continue
        matched = _match_release(it.get('release_name'))
        if not matched:
            continue
        name_ko, impact = matched
        if name_ko in seen_names:         # 같은 지표는 이번 주 첫 발표일만
            continue
        seen_names.add(name_ko)
        events.append({'date': d, 'name': name_ko, 'kind': 'indicator', 'impact': impact})
    print(f'[EconCal] FRED {monday}~{sunday}: 원본 {len(raw)}건 중 주요 지표 {len(events)}건 매칭 — HTTP 200')
    return events, 'ok'


def fomc_events(monday, sunday, path=FOMC_PATH):
    """고정 일정 JSON에서 해당 주의 FOMC 이벤트(금리 결정·의사록 공개)를 계산. 키 불필요."""
    try:
        with open(path, encoding='utf-8') as f:
            meetings = json.load(f).get('meetings') or []
    except (OSError, ValueError) as e:
        print(f'[EconCal] FOMC 일정 파일 로드 실패({path}): {e}')
        return []
    lo, hi = monday.isoformat(), sunday.isoformat()
    out = []
    for m in meetings:
        start, end = m.get('start') or '', m.get('end') or ''
        if not end:
            continue
        label = f'{int(start[5:7])}/{int(start[8:10])}~{int(end[8:10])}' if start else end
        if lo <= end <= hi:
            out.append({'date': end, 'name': f'FOMC 금리 결정({label} 회의)',
                        'kind': 'fomc', 'impact': 'high'})
        minutes = (datetime.strptime(end, '%Y-%m-%d') + timedelta(days=21)).strftime('%Y-%m-%d')
        if lo <= minutes <= hi:
            out.append({'date': minutes, 'name': f'FOMC 의사록 공개({label} 회의분)',
                        'kind': 'fomc', 'impact': 'medium'})
    if out:
        print(f'[EconCal] FOMC {monday}~{sunday}: {len(out)}건')
    return out


def collect_week():
    """
    이번 주 주요 일정 전체.
    반환: (events(날짜순), status, fred_ok) — FRED 실패여도 FOMC 일정은 항상 포함.
    """
    monday, sunday = week_range()
    fred, status = fetch_fred_releases(monday, sunday)
    fomc = fomc_events(monday, sunday)
    events = sorted(fred + fomc, key=lambda x: (x['date'], x['kind']))
    return events, status, status == 'ok'
