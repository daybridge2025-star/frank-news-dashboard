"""
KRX 데이터 스냅샷 수집 — GitHub Actions에서 하루 2회 실행 (KST 05:50 / 15:50)
GitHub Actions: .github/workflows/krx_snapshot.yml

매크로 전략 브리핑(reports/macro-strategy-briefing.html) 작성 시 참고 자료로 쓴다.

데이터 소스 2개 병행:
  1. Open API(utils.krx, AUTH_KEY) — 지수·종목 시세, 서비스 이용신청 승인 후 동작
  2. pykrx(utils.krx_scrape, data.krx.co.kr) — 투자자별 수급(로그인 필요) +
     개별종목 시세 폴백(로그인 없이 동작, 실측 확인)

우선순위: 시세는 Open API 시도 → 비면 pykrx 폴백.
투자자별 수급은 pykrx 전담(Open API 카탈로그에 없음).
자격증명/승인이 없으면 데이터를 지어내지 않고 '미확보'로 남긴다.
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

from utils.krx import get_kospi_index, get_kosdaq_index, get_kospi_stock
from utils.krx_scrape import get_investor_flow, get_stock_ohlcv

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
    최근 영업일 판별 — 오늘부터 최대 7일 역산.
    Open API 키가 있으면 지수 응답으로, 없으면 pykrx 개별종목 시세(무로그인)로 판별한다.
    반환: (기준일자 'YYYYMMDD', kospi_index_rows) — pykrx 경로면 index rows는 빈 리스트.
    """
    now = datetime.now(KST)
    for i in range(7):
        d = (now - timedelta(days=i)).strftime('%Y%m%d')
        if auth_key:
            rows = get_kospi_index(d, auth_key)
            if rows:
                return d, rows
        if get_stock_ohlcv(d, '005930'):  # pykrx, 로그인 없이 동작
            return d, []
    return now.strftime('%Y%m%d'), []


def main():
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')
    print(f'[{now_str}] KRX 데이터 스냅샷 수집 시작')
    print('=' * 50)

    auth_key = os.environ.get('KRX_AUTH_KEY', '')
    if not auth_key:
        print('KRX_AUTH_KEY 없음 — Open API 시세는 건너뛰고 pykrx로만 수집')

    bas_dd, kospi_rows = _latest_business_day(auth_key)
    print(f'기준일자: {bas_dd}')

    kosdaq_rows = get_kosdaq_index(bas_dd, auth_key) if auth_key else []
    print(f'KOSPI 지수 로우: {len(kospi_rows)}건 / KOSDAQ 지수 로우: {len(kosdaq_rows)}건')

    stocks = {}
    for code, name in WATCHLIST.items():
        rows = get_kospi_stock(bas_dd, isu_cd=code, auth_key=auth_key) if auth_key else []
        entry = {'name': name, 'source': 'openapi', 'raw': rows}
        if not rows:
            # Open API 미승인 등으로 비면 pykrx 폴백(로그인 없이 개별종목 시세 동작)
            ohlcv = get_stock_ohlcv(bas_dd, code)
            if ohlcv:
                entry = {'name': name, 'source': 'pykrx', 'ohlcv': ohlcv}
        stocks[code] = entry
        got = len(rows) if rows else (1 if entry.get('ohlcv') else 0)
        print(f"  {name}({code}): {got}건 [{entry['source']}]")

    snapshot = {
        'bas_dd': bas_dd,
        'fetched_at': now_str,
        'kospi_index': kospi_rows,
        'kosdaq_index': kosdaq_rows,
        'stocks': stocks,
        'investor_flow': get_investor_flow(bas_dd),  # pykrx (KRX_ID/PW 있을 때만 채워짐)
    }

    flow = snapshot['investor_flow']
    print(f"투자자 수급: {flow.get('status')}"
          + (f" — {flow.get('reason')}" if flow.get('status') != 'ok' else ''))

    os.makedirs('data', exist_ok=True)
    dated_path = f'data/krx_snapshot_{bas_dd}.json'
    latest_path = 'data/krx_snapshot_latest.json'
    for path in (dated_path, latest_path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)

    print('=' * 50)
    print(f'완료: {dated_path}, {latest_path} 저장')


if __name__ == '__main__':
    main()
