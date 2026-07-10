"""
임시 진단 스크립트 — MTD/YTD 누적 구현 방식 결정을 위한 1회성 실측.
결과 확인 후 이 파일과 워크플로우는 삭제한다(프로덕션 코드 아님).
"""
import sys
import time
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from pykrx import stock


def probe(label, fromdate, todate, market='KOSPI'):
    t0 = time.time()
    try:
        df = stock.get_market_trading_value_by_investor(fromdate, todate, market)
        dt = time.time() - t0
        print(f'[{label}] {market} {fromdate}~{todate}: rows={len(df)} time={dt:.1f}s')
        print('  columns:', list(df.columns))
        print('  index:', list(df.index)[:5], '...' if len(df.index) > 5 else '')
        for idx, row in df.reset_index().iterrows():
            print('  ', row.to_dict())
    except Exception as e:
        print(f'[{label}] FAIL: {e!r}')


print('=== 1) 짧은 범위(이번달 초~오늘, MTD 흉내) ===')
probe('MTD-ish', '20260701', '20260709', 'KOSPI')

print('\n=== 2) 긴 범위(연초~오늘, YTD 흉내, 약 6개월) ===')
probe('YTD-ish', '20260102', '20260709', 'KOSPI')

print('\n=== 3) 코스닥도 동일 패턴인지 ===')
probe('KOSDAQ MTD-ish', '20260701', '20260709', 'KOSDAQ')

print('\n=== 4) 순매수 상위(기간 누적)도 되는지 ===')
try:
    t0 = time.time()
    df = stock.get_market_net_purchases_of_equities('20260701', '20260709', 'KOSPI', '외국인')
    print(f'  rows={len(df)} time={time.time()-t0:.1f}s, columns={list(df.columns)}')
    print(df.reset_index().head(3).to_dict(orient='records'))
except Exception as e:
    print(f'  FAIL: {e!r}')
