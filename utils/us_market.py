"""
미국·글로벌 시세/지표 수집 — 브리핑 핵심 지표 카드의 '값' 자동화용.

소스 (app.py에서 이미 검증된 패턴 재사용):
  1. Yahoo Finance chart API (키 불필요) — 지수·선물·환율.
     app.py가 USDKRW=X·^W5000에 쓰는 것과 같은 엔드포인트.
  2. FRED (FRED_API_KEY 필요) — 국채금리·HY 스프레드·연준 타깃금리.
     app.py fetch_fred_data()와 같은 API. 키 없으면 해당 값은 미확보로 남는다.

원칙: 실패한 항목은 지어내지 않고 결과에서 뺀다 — 렌더러가 해당 마커를 건너뛰어
기존 표기가 유지된다(브리핑의 '미확보' 원칙과 동일).
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta

_UA = {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36')}

# 카드에 쓰는 심볼 + 참고용 미국 3대 지수(카드는 없지만 세션이 인용할 수 있게 저장)
YAHOO_SYMBOLS = {
    'vix':    '^VIX',
    'usdkrw': 'KRW=X',
    'wti':    'CL=F',
    'gold':   'GC=F',
    'copper': 'HG=F',
    'dxy':    'DX-Y.NYB',
    'sp500':  '^GSPC',
    'nasdaq': '^IXIC',
    'dow':    '^DJI',
}

FRED_SERIES = {
    't10y':      'DGS10',          # 미 10년물 (%)
    't2y':       'DGS2',           # 미 2년물 (%)
    'hy_oas':    'BAMLH0A0HYM2',   # HY OAS (%p)
    'fed_upper': 'DFEDTARU',       # 연준 타깃 상단 (%)
    'fed_lower': 'DFEDTARL',       # 연준 타깃 하단 (%)
}

_ET = timezone(timedelta(hours=-4))  # 미 동부 하절기(EDT) — 날짜 라벨용 근사


def fetch_yahoo(symbols=None):
    """
    Yahoo chart API에서 최근 종가·전일대비를 수집.
    반환: {key: {'price': float, 'prev': float|None, 'change_pct': float|None,
                 'asof': 'YYYY-MM-DD'(현지 근사)}} — 실패 항목은 제외.
    """
    out = {}
    for key, sym in (symbols or YAHOO_SYMBOLS).items():
        try:
            r = requests.get(
                f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}',
                params={'range': '5d', 'interval': '1d'},
                headers=_UA, timeout=10)
            if not r.ok:
                print(f'[Yahoo] {key}({sym}) HTTP {r.status_code}')
                continue
            res = (r.json().get('chart', {}).get('result') or [None])[0]
            if not res:
                continue
            meta = res.get('meta', {})
            price = meta.get('regularMarketPrice')
            if price is None:
                continue
            ts = meta.get('regularMarketTime')
            # 전일 종가: 봉 날짜와 시세 날짜를 비교해 고른다 —
            # 마지막 봉이 시세와 같은 날이면 그 앞 봉이 전일, 다르면 마지막 봉이 전일.
            raw_ts = res.get('timestamp') or []
            raw_cl = (res.get('indicators', {}).get('quote', [{}])[0].get('close') or [])
            bars = [(t, c) for t, c in zip(raw_ts, raw_cl) if c is not None]
            prev = None
            if bars and ts:
                mkt_date = datetime.fromtimestamp(ts, tz=_ET).date()
                last_date = datetime.fromtimestamp(bars[-1][0], tz=_ET).date()
                if last_date == mkt_date and len(bars) >= 2:
                    prev = bars[-2][1]
                else:
                    prev = bars[-1][1]
            if prev is None:
                prev = meta.get('chartPreviousClose')
            chg = (price / prev - 1) * 100 if prev else None
            asof = (datetime.fromtimestamp(ts, tz=_ET).strftime('%Y-%m-%d') if ts else None)
            out[key] = {'price': float(price),
                        'prev': float(prev) if prev is not None else None,
                        'change_pct': round(chg, 2) if chg is not None else None,
                        'asof': asof}
        except Exception as e:
            print(f'[Yahoo] {key}({sym}) 실패: {e}')
        time.sleep(0.3)
    return out


# 연초 대비 바를 그리는 카드(미국 3대 지수) — range=ytd 추가 조회 대상
YTD_KEYS = ('sp500', 'nasdaq', 'dow')


def enrich_ytd(yahoo):
    """3대 지수에 연초 종가·연중(장중) 고점·연초 대비 등락률을 보강한다.
    실패해도 기존 price/change_pct는 유지(해당 카드의 바만 미표시)."""
    for key in YTD_KEYS:
        item = yahoo.get(key)
        if not item:
            continue
        sym = YAHOO_SYMBOLS[key]
        try:
            r = requests.get(
                f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}',
                params={'range': 'ytd', 'interval': '1d'},
                headers=_UA, timeout=10)
            if not r.ok:
                print(f'[Yahoo/YTD] {key} HTTP {r.status_code}')
                continue
            res = (r.json().get('chart', {}).get('result') or [None])[0]
            if not res:
                continue
            q = (res.get('indicators', {}).get('quote', [{}])[0])
            closes = [c for c in (q.get('close') or []) if c is not None]
            highs = [x for x in (q.get('high') or []) if x is not None]
            if not closes:
                continue
            start = closes[0]
            high = max(highs) if highs else max(closes)
            item['ytd_start'] = round(float(start), 2)
            item['ytd_high'] = round(float(high), 2)
            item['ytd_change_pct'] = round((item['price'] / start - 1) * 100, 1)
        except Exception as e:
            print(f'[Yahoo/YTD] {key} 실패: {e}')
        time.sleep(0.3)
    return yahoo


def fetch_fred(series=None):
    """
    FRED 최신 관측치 수집 (app.py fetch_fred_data 패턴).
    FRED_API_KEY 없으면 빈 dict — 해당 카드는 미확보 유지.
    반환: {key: {'value': float, 'date': 'YYYY-MM-DD'}}
    """
    api_key = os.environ.get('FRED_API_KEY', '')
    if not api_key:
        print('[FRED] FRED_API_KEY 없음 — 금리·스프레드 카드는 미확보 유지')
        return {}
    out = {}
    for key, sid in (series or FRED_SERIES).items():
        try:
            r = requests.get(
                'https://api.stlouisfed.org/fred/series/observations',
                params={'series_id': sid, 'api_key': api_key, 'file_type': 'json',
                        'sort_order': 'desc', 'limit': 30},
                headers=_UA, timeout=10)
            if not r.ok:
                print(f'[FRED] {key}({sid}) HTTP {r.status_code}')
                continue
            for o in r.json().get('observations', []):
                if o.get('value') not in (None, '.', ''):
                    out[key] = {'value': float(o['value']), 'date': o.get('date')}
                    break
        except Exception as e:
            print(f'[FRED] {key}({sid}) 실패: {e}')
        time.sleep(0.25)
    return out


def collect():
    """전체 수집 — {'yahoo': {...}, 'fred': {...}}. 부분 실패 허용."""
    yahoo = fetch_yahoo()
    yahoo = enrich_ytd(yahoo)
    fred = fetch_fred()
    print(f'[US] Yahoo {len(yahoo)}/{len(YAHOO_SYMBOLS)}개 · FRED {len(fred)}/{len(FRED_SERIES)}개 수집')
    return {'yahoo': yahoo, 'fred': fred}
