"""
이번 주 주요 경제 캘린더 수집 → data/econ_calendar_latest.json

GitHub Actions에서 하루 1회(krx_snapshot.yml) 실행. 소스는 FRED 릴리스 캘린더
(FRED_API_KEY) + 연준 공식 FOMC 고정 일정(data/fomc_schedule.json, 키 불필요).
status와 fred_ok를 항상 함께 저장해 build_briefing.py가 "왜 비었는지/일부인지"
(키 없음·FRED 오류·정상인데 이번 주 일정 없음)를 구분할 수 있게 한다 —
값을 지어내지 않는 대신, 실패 이유는 투명하게 남긴다.
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


def main():
    events, status, fred_ok = econ_calendar.collect_week()
    monday, sunday = econ_calendar.week_range()
    out = {
        'status': status,
        'fred_ok': fred_ok,
        'week_start': monday.isoformat(),
        'week_end': sunday.isoformat(),
        'events': events,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'저장 완료: {OUT_PATH} (status={status}, fred_ok={fred_ok}, {len(events)}건)')


if __name__ == '__main__':
    main()
