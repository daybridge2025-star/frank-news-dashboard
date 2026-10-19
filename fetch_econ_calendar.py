"""
이번 주 주요 경제 캘린더 수집 → data/econ_calendar_latest.json

GitHub Actions에서 하루 1회(krx_snapshot.yml) 실행. 소스는 FRED 릴리스 캘린더
(FRED_API_KEY) + 연준 공식 FOMC 고정 일정(data/fomc_schedule.json, 키 불필요).
status와 fred_ok를 항상 함께 저장해 build_briefing.py가 "왜 비었는지/일부인지"
(키 없음·FRED 오류·정상인데 이번 주 일정 없음)를 구분할 수 있게 한다 —
값을 지어내지 않는 대신, 실패 이유는 투명하게 남긴다.

FRED가 막힌 날(fred_ok=False)에는 직전 성공 파일이 *같은 주* 것일 때만 그 지표 일정을 이월한다 —
주간 일정은 하루 사이에 바뀌지 않으므로 "일정이 사라졌다"고 오해시키는 것보다 낫다. 이월분은
stale_events=True와 events_fetched_at(그 일정을 실제로 받은 시각)으로 표시해 렌더러(일정 박스)가
"FRED 응답이 늦어 … 기준 일정이에요"라고 밝히게 한다. 다음 주로 넘어가면 이월하지 않는다.
"""

import json
import os
import sys
from datetime import datetime, timezone

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(__file__))
from utils import econ_calendar

OUT_PATH = 'data/econ_calendar_latest.json'


def _load_prev(path=None):
    try:
        with open(path or OUT_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def carry_over(events, fred_ok, monday, prev, now_iso):
    """FRED 실패 시 직전 성공 파일(같은 주)의 지표 일정을 이월한다. (events, stale_events, events_fetched_at) 반환.
    prev 자체가 이월분이면 events_fetched_at을 그대로 물려받아 '실제로 받은 시각'이 끝까지 보존된다."""
    if fred_ok or not prev or prev.get('week_start') != monday.isoformat():
        return events, False, now_iso
    carried = [e for e in (prev.get('events') or []) if e.get('kind') != 'fomc']   # FOMC는 방금 계산한 것을 쓴다
    if not carried:
        return events, False, now_iso
    merged = sorted(carried + list(events), key=lambda e: (e.get('date', ''), e.get('kind', '')))
    return merged, True, prev.get('events_fetched_at') or prev.get('fetched_at') or now_iso


def main():
    events, status, fred_ok = econ_calendar.collect_week()
    monday, sunday = econ_calendar.week_range()
    now_iso = datetime.now(timezone.utc).isoformat()
    events, stale, events_at = carry_over(events, fred_ok, monday, _load_prev(), now_iso)
    out = {
        'status': status,
        'fred_ok': fred_ok,
        'week_start': monday.isoformat(),
        'week_end': sunday.isoformat(),
        'events': events,
        'stale_events': stale,
        'events_fetched_at': events_at,
        'fetched_at': now_iso,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'저장 완료: {OUT_PATH} (status={status}, fred_ok={fred_ok}, {len(events)}건'
          f'{", 직전 성공분 이월 — " + events_at if stale else ""})')


if __name__ == '__main__':
    main()
