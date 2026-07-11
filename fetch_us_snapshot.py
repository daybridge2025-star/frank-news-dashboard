"""
미국·글로벌 지표 스냅샷 수집 — GitHub Actions에서 KRX 수집 직후 실행.
출력: data/us_snapshot_latest.json (마커 주입은 build_briefing.py 담당)

부분 실패 허용: 수집된 항목만 저장하고, 실패 항목은 렌더러가 건너뛰어
브리핑의 기존 표기가 유지된다. 단 하나도 못 모으면 기존 파일을 덮어쓰지 않는다.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import json
from datetime import datetime
import pytz

from utils.us_market import collect

KST = pytz.timezone('Asia/Seoul')
OUT_PATH = 'data/us_snapshot_latest.json'


def main():
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')
    print(f'[{now_str}] 미국 지표 스냅샷 수집 시작')
    data = collect()
    if not data.get('yahoo') and not data.get('fred'):
        print('오류: Yahoo·FRED 모두 실패 — 기존 스냅샷을 덮어쓰지 않고 종료')
        sys.exit(1)
    data['fetched_at'] = now_str
    os.makedirs('data', exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f'완료: {OUT_PATH} 저장')


if __name__ == '__main__':
    main()
