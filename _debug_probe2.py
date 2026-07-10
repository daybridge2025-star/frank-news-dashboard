"""
임시 진단 스크립트 — '직전 영업일=오늘(장마감 후)'이 실제로 되는지 소스별 확인.
확인 후 이 파일과 워크플로우는 삭제한다(프로덕션 코드 아님).
"""
import sys
import os
import requests
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

TODAY = '20260710'
YEST = '20260709'

print(f'=== 1) Open API 지수 — 오늘({TODAY}) 데이터 있나? ===')
key = os.environ.get('KRX_AUTH_KEY', '')
for d in (TODAY, YEST):
    r = requests.get('http://data-dbg.krx.co.kr/svc/apis/idx/kospi_dd_trd',
                      params={'basDd': d}, headers={'AUTH_KEY': key}, timeout=15)
    rows = r.json().get('OutBlock_1', [])
    print(f'  {d}: HTTP {r.status_code}, rows={len(rows)}')

print(f'\n=== 2) pykrx 투자자별 수급 — 오늘({TODAY}) 데이터 있나? (로그인 필요) ===')
from pykrx import stock
for d in (TODAY, YEST):
    try:
        df = stock.get_market_trading_value_by_investor(d, d, 'KOSPI')
        print(f'  {d}: rows={len(df)}', '(있음)' if not df.empty else '(비어있음)')
    except Exception as e:
        print(f'  {d}: FAIL {e!r}')

print(f'\n=== 3) pykrx 외국인 순매수 상위 — 오늘({TODAY}) 데이터 있나? ===')
for d in (TODAY, YEST):
    try:
        df = stock.get_market_net_purchases_of_equities(d, d, 'KOSPI', '외국인')
        print(f'  {d}: rows={len(df)}', '(있음)' if not df.empty else '(비어있음)')
    except Exception as e:
        print(f'  {d}: FAIL {e!r}')
