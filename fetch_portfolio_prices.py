"""
포트폴리오 보유 종목 '직전 종가' 수집 — 포트폴리오 탭 자동 갱신용.
입력: data/portfolio_holdings.json (사용자 캡처 기준 보유내역 — 수량·평단의 원본)
출력: data/portfolio_prices_latest.json (마커 주입·평가 계산은 build_briefing.py 담당)

'직전 종가' 규칙: 마지막으로 *마감된* 세션의 종가만 쓴다 — 장중(marketState=REGULAR)에
실행되면 진행 중인 당일 봉을 버리고 그 앞 봉을 쓴다. 한국 종목도 야후(.KS/.KQ)로
일원화(pykrx는 폴백) — Action·세션 어디서든 같은 코드가 돈다.

부분 실패 허용: 수집된 종목만 저장, 실패 종목은 렌더러가 '미확보'로 표기.
단 하나도 못 모으면 기존 파일을 덮어쓰지 않는다.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import json
import time
from datetime import datetime

import requests
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
HOLD_PATH = 'data/portfolio_holdings.json'
OUT_PATH = 'data/portfolio_prices_latest.json'
_UA = {'User-Agent': 'Mozilla/5.0'}


def _last_closed_bar(symbol):
    """야후 chart API에서 마지막 '마감된' 세션의 (종가, 날짜문자열)을 반환. 실패 시 None."""
    r = requests.get(
        f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}',
        params={'range': '10d', 'interval': '1d'}, headers=_UA, timeout=12)
    if not r.ok:
        print(f'[Yahoo] {symbol} HTTP {r.status_code}')
        return None
    res = (r.json().get('chart', {}).get('result') or [None])[0]
    if not res:
        return None
    meta = res.get('meta', {})
    tzname = meta.get('exchangeTimezoneName') or 'America/New_York'
    tz = ZoneInfo(tzname)
    raw_ts = res.get('timestamp') or []
    raw_cl = (res.get('indicators', {}).get('quote', [{}])[0].get('close') or [])
    bars = [(t, c) for t, c in zip(raw_ts, raw_cl) if c is not None]
    if not bars:
        return None
    # 장중이면 마지막 봉은 진행 중 → 버린다.
    # 판정: currentTradingPeriod.regular(당일 정규장 시작·종료 ts) 기준 — 지금이 그 구간 안이고
    # 마지막 봉이 현지 오늘 날짜면 라이브 봉이다. (marketState는 이 엔드포인트에서 None인 경우가
    # 실측됐고, 날짜 비교만으로는 '마감 직후'의 유효한 당일 봉까지 버리게 되므로 이 방식이 정확하다.)
    now_ts = time.time()
    reg = (meta.get('currentTradingPeriod') or {}).get('regular') or {}
    in_session = (reg.get('start') or 0) <= now_ts < (reg.get('end') or 0)
    last_date = datetime.fromtimestamp(bars[-1][0], tz=tz).date()
    if in_session and last_date == datetime.now(tz).date():
        bars = bars[:-1]
    if not bars:
        return None
    ts, close = bars[-1]
    return float(close), datetime.fromtimestamp(ts, tz=tz).strftime('%Y-%m-%d')


def _pykrx_fallback(ticker):
    """야후 실패 시 국내 종목 pykrx 폴백(Action 환경에서만 성립할 수 있음)."""
    try:
        from pykrx import stock as krx
        from datetime import timedelta
        end = datetime.now(KST).strftime('%Y%m%d')
        start = (datetime.now(KST) - timedelta(days=14)).strftime('%Y%m%d')
        df = krx.get_market_ohlcv_by_date(start, end, ticker)
        if df is None or df.empty:
            df = krx.get_etf_ohlcv_by_date(start, end, ticker)
        if df is None or df.empty:
            return None
        # 당일 장중 행이 섞일 수 있으므로 오늘 날짜 행은 제외
        today = datetime.now(KST).date()
        df = df[[d.date() < today or datetime.now(KST).hour >= 16 for d in df.index]]
        if df.empty:
            return None
        row = df.iloc[-1]
        return float(row['종가']), df.index[-1].strftime('%Y-%m-%d')
    except Exception as e:
        print(f'[pykrx] {ticker} 폴백 실패: {e}')
        return None


def main():
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')
    print(f'[{now_str}] 포트폴리오 가격 수집 시작')
    if not os.path.exists(HOLD_PATH):
        print(f'보유내역 없음: {HOLD_PATH} — 종료')
        return
    with open(HOLD_PATH, encoding='utf-8') as f:
        hold = json.load(f)

    symbols = {}  # ticker -> (yahoo_symbol, market)
    for b in hold.get('brokers', []):
        for h in b.get('holdings', []):
            symbols[h['ticker']] = (h.get('yahoo') or h['ticker'], h.get('market', 'US'))

    prices = {}
    for ticker, (ysym, market) in symbols.items():
        got = None
        try:
            got = _last_closed_bar(ysym)
        except Exception as e:
            print(f'[Yahoo] {ysym} 실패: {e}')
        if got is None and market == 'KR':
            got = _pykrx_fallback(ticker)
        if got:
            close, asof = got
            prices[ticker] = {'close': close, 'asof': asof}
            print(f'  {ticker:8s} {close:>12,.2f} ({asof})')
        else:
            print(f'  {ticker:8s} 미확보')
        time.sleep(0.3)

    if not prices:
        print('오류: 전 종목 수집 실패 — 기존 파일을 덮어쓰지 않고 종료')
        sys.exit(1)
    out = {'fetched_at': now_str, 'prices': prices}
    os.makedirs('data', exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'완료: {OUT_PATH} 저장 ({len(prices)}/{len(symbols)}종목)')


if __name__ == '__main__':
    main()
