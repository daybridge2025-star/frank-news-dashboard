"""
KRX 데이터 스냅샷 수집 — GitHub Actions에서 하루 2회 실행 (KST 05:50 / 15:50)
GitHub Actions: .github/workflows/krx_snapshot.yml

매크로 전략 브리핑(reports/macro-strategy-briefing.html) 작성 시 참고 자료로 쓴다.

이 스크립트가 채우는 것: KOSPI/KOSDAQ 지수 시세, 관심종목 일별매매정보(공식 수치)
이 스크립트가 못 채우는 것: 투자자별(외국인/기관/개인/연기금) 순매수, 업종별 수급
  → utils/krx.get_investor_flow() 참고 — pykrx/회원 로그인 연동은 별도 작업으로 남겨둠
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

# Windows 로컬 콘솔은 기본 cp949라 '—' 등 유니코드 문자에서 죽는다 — UTF-8로 강제
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import json
from datetime import datetime, timedelta
import pytz

from utils.krx import get_kospi_index, get_kosdaq_index, get_kospi_stock, get_investor_flow

KST = pytz.timezone('Asia/Seoul')

# 전략 A/B 한국 20% 배분에 등장하는 관심종목 (reports/macro-strategy-briefing.html 기준)
WATCHLIST = {
    '005930': '삼성전자',
    '000660': 'SK하이닉스',
    '267260': 'HD현대일렉트릭',
    '010120': 'LS ELECTRIC',
    '012450': '한화에어로스페이스',
    '064350': '현대로템',
}


def _latest_business_day(auth_key):
    """
    별도 휴장일 API를 붙이기 전까지의 임시 방편 —
    오늘부터 최대 5일 역산하며 지수 데이터가 처음 나오는 날을 최근 영업일로 간주한다.
    """
    now = datetime.now(KST)
    for i in range(5):
        d = (now - timedelta(days=i)).strftime('%Y%m%d')
        rows = get_kospi_index(d, auth_key)
        if rows:
            return d, rows
    return now.strftime('%Y%m%d'), []


def main():
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')
    print(f'[{now_str}] KRX 데이터 스냅샷 수집 시작')
    print('=' * 50)

    auth_key = os.environ.get('KRX_AUTH_KEY', '')
    if not auth_key:
        print('KRX_AUTH_KEY 없음 — 종료')
        sys.exit(1)

    bas_dd, kospi_rows = _latest_business_day(auth_key)
    print(f'기준일자: {bas_dd}')

    kosdaq_rows = get_kosdaq_index(bas_dd, auth_key)
    print(f'KOSPI 지수 로우: {len(kospi_rows)}건 / KOSDAQ 지수 로우: {len(kosdaq_rows)}건')

    stocks = {}
    for code, name in WATCHLIST.items():
        rows = get_kospi_stock(bas_dd, isu_cd=code, auth_key=auth_key)
        stocks[code] = {'name': name, 'raw': rows}
        print(f'  {name}({code}): {len(rows)}건')

    snapshot = {
        'bas_dd': bas_dd,
        'fetched_at': now_str,
        'kospi_index': kospi_rows,
        'kosdaq_index': kosdaq_rows,
        'stocks': stocks,
        'investor_flow': get_investor_flow(),  # 항상 '미확보' — pykrx 연동 전까지
    }

    os.makedirs('data', exist_ok=True)
    dated_path = f'data/krx_snapshot_{bas_dd}.json'
    latest_path = 'data/krx_snapshot_latest.json'
    for path in (dated_path, latest_path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print('=' * 50)
    print(f'완료: {dated_path}, {latest_path} 저장')


if __name__ == '__main__':
    main()
