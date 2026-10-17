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

import io
import os
import re
import subprocess
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
    price: 수집 시각이 17시 ET 이후이고 같은 날 일봉이 있으면 그 일봉 종가, 아니면 수집 순간의 시세.
    선물·환율(WTI·금·구리·원/달러)은 저녁에도 야간 거래가 이어져 regularMarketPrice가 '종가'가 아니라
    수집 순간의 호가이기 때문이다(2026-10-15 WTI: 호가 96.15 vs 확정 종가 97.97 — 하루 뒤에야 드러났다).
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
                mkt_dt = datetime.fromtimestamp(ts, tz=_ET)
                mkt_date = mkt_dt.date()
                last_date = datetime.fromtimestamp(bars[-1][0], tz=_ET).date()
                if last_date == mkt_date and len(bars) >= 2:
                    prev = bars[-2][1]
                    if mkt_dt.hour >= 17:           # 정규장·원자재 정산 이후 → 오늘 일봉 종가가 확정값
                        price = bars[-1][1]
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


# ── 공포탐욕지수(CNN) ────────────────────────────────────────────────────
# 2026-10-15 실측: 헤더 없이(또는 User-Agent만) 부르면 HTTP 418 "I'm a teapot. You're a bot."을 돌려주지만
# 브라우저가 보내는 Origin/Referer를 함께 주면 200이 온다. 막히면 GitHub 미러 CSV로 폴백한다.
_CNN_FNG_URL = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata'
_CNN_HEADERS = {**_UA,
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Origin': 'https://www.cnn.com',
                'Referer': 'https://www.cnn.com/markets/fear-and-greed'}
_FNG_MIRROR_URL = 'https://raw.githubusercontent.com/whit3rabbit/fear-greed-data/main/fear-greed.csv'


def _us_session_date(ts_utc):
    """CNN 값이 계산된 시각(UTC) → 그 값이 반영한 미국 정규장 날짜(ISO). 정규장 마감(16시 ET) 전이면 직전 거래일,
    주말이면 금요일로 돌린다(공휴일은 고려하지 않는 근사)."""
    et = ts_utc.astimezone(_ET)
    d = et.date()
    if et.hour < 16:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def fetch_fear_greed():
    """
    CNN 공포탐욕지수(0~100). 반환: {'score','rating','asof','prev_close','prev_week','prev_month','source'} 또는 None.
    CNN 실패 → 미러 CSV(Date,Fear Greed,Rating)의 마지막 행 → 둘 다 실패하면 None(렌더러가 기존 표기를 유지).
    """
    try:
        r = requests.get(_CNN_FNG_URL, headers=_CNN_HEADERS, timeout=15)
        if r.ok:
            fg = (r.json().get('fear_and_greed') or {})
            score = fg.get('score')
            if isinstance(score, (int, float)):
                ts = datetime.fromisoformat(fg['timestamp'])
                out = {'score': round(float(score), 1),
                       'rating': str(fg.get('rating') or '').strip().lower(),
                       'asof': _us_session_date(ts), 'source': 'CNN'}
                for src, dst in (('previous_close', 'prev_close'), ('previous_1_week', 'prev_week'),
                                 ('previous_1_month', 'prev_month')):
                    v = fg.get(src)
                    if isinstance(v, (int, float)):
                        out[dst] = round(float(v), 1)
                return out
        else:
            print(f'[FearGreed] CNN HTTP {r.status_code}')
    except Exception as e:
        print(f'[FearGreed] CNN 실패: {e}')
    try:
        r = requests.get(_FNG_MIRROR_URL, timeout=20)
        if r.ok:
            rows = [ln.split(',') for ln in r.text.strip().splitlines()[1:] if ln.count(',') >= 2]
            if rows:
                out = {'score': round(float(rows[-1][1]), 1), 'rating': rows[-1][2].strip().lower(),
                       'asof': rows[-1][0].strip(), 'source': 'mirror'}
                if len(rows) >= 2:
                    out['prev_close'] = round(float(rows[-2][1]), 1)
                print('[FearGreed] CNN 대신 미러 CSV 사용')
                return out
    except Exception as e:
        print(f'[FearGreed] 미러 실패: {e}')
    return None


# ── S&P 500 선행 PER(FactSet Earnings Insight) ───────────────────────────
# FactSet이 매주 금요일 공개 PDF를 낸다(URL 끝이 MMDDYY). 첫 쪽 요약에 "The forward 12-month P/E ratio for the
# S&P 500 is 19.1. This P/E ratio is below the 5-year average (19.8) but equal to the 10-year average (19.1)."가 있다.
_FACTSET_URL = ('https://advantage.factset.com/hubfs/Website/Resources%20Section/Research%20Desk/'
                'Earnings%20Insight/EarningsInsight_{d}.pdf')
_NUM = r'(\d+(?:\.\d+)?)'
# 추출기마다 공백이 다르다(2026-10-15 실측: pypdf는 "forward 12 -month"처럼 하이픈 앞에 공백을 넣어 옛 패턴이 안 맞았다).
# 그래서 공백을 전부 지운 문자열에서 공백 없는 패턴으로 찾는다.
_PE_RE = re.compile(r'forward12-?monthP/Eratiofor(?:the)?S&P500is' + _NUM, re.I)
_AVG5_RE = re.compile(r'5-?yearaverage(?:of)?\(?' + _NUM, re.I)
_AVG10_RE = re.compile(r'10-?yearaverage(?:of)?\(?' + _NUM, re.I)


def parse_factset_pe(text):
    """PDF 본문 텍스트 → {'forward_pe','avg5','avg10'}(없는 항목은 생략) 또는 None. 같은 문장 근처(200자)에서만 평균을 찾는다."""
    s = re.sub(r'\s+', '', re.sub(r'[\u2010-\u2015\u2212]', '-', text or ''))
    m = _PE_RE.search(s)
    if not m:
        return None
    out = {'forward_pe': float(m.group(1))}
    seg = s[m.end(): m.end() + 200]
    a5, a10 = _AVG5_RE.search(seg), _AVG10_RE.search(seg)
    if a5:
        out['avg5'] = float(a5.group(1))
    if a10:
        out['avg10'] = float(a10.group(1))
    return out


def _pdf_texts(content):
    """PDF 바이트 → 앞 4쪽 텍스트 후보들(pypdf, poppler pdftotext 순). 한쪽이 문장을 못 살려도 다음 것으로 재시도하게 제너레이터로 낸다."""
    try:
        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(content))
        yield ' '.join((rd.pages[i].extract_text() or '') for i in range(min(4, len(rd.pages))))
    except Exception as e:
        print(f'[Valuation] pypdf 사용 불가({e})')
    try:
        p = subprocess.run(['pdftotext', '-l', '4', '-', '-'], input=content, capture_output=True, timeout=60)
        yield p.stdout.decode('utf-8', 'replace')
    except Exception as e:
        print(f'[Valuation] pdftotext 사용 불가: {e}')


def fetch_valuation(today=None):
    """
    최근 2주 안의 금요일 FactSet 보고서 중 가장 최신 것에서 S&P 500 선행 PER를 읽는다.
    반환: {'forward_pe','avg5','avg10','asof','source'} 또는 None(렌더러가 기존 표기를 유지).
    """
    today = today or datetime.now(timezone.utc).date()
    for back in range(15):
        d = today - timedelta(days=back)
        if d.weekday() != 4:      # 금요일 발간
            continue
        try:
            r = requests.get(_FACTSET_URL.format(d=d.strftime('%m%d%y')), headers=_UA, timeout=30)
            if not r.ok or not r.content.startswith(b'%PDF'):
                continue
            pe = next((x for x in (parse_factset_pe(tx) for tx in _pdf_texts(r.content)) if x), None)
            if pe:
                pe.update(asof=d.isoformat(), source='FactSet Earnings Insight')
                return pe
        except Exception as e:
            print(f'[Valuation] {d} 실패: {e}')
    return None


def collect():
    """전체 수집 — {'yahoo': {...}, 'fred': {...}, 'fear_greed': {...}, 'valuation': {...}}. 부분 실패 허용
    (못 구한 항목은 키 자체를 뺀다 → 렌더러가 해당 카드의 기존 표기와 그 기준일 배지를 그대로 둔다)."""
    yahoo = fetch_yahoo()
    yahoo = enrich_ytd(yahoo)
    fred = fetch_fred()
    print(f'[US] Yahoo {len(yahoo)}/{len(YAHOO_SYMBOLS)}개 · FRED {len(fred)}/{len(FRED_SERIES)}개 수집')
    out = {'yahoo': yahoo, 'fred': fred}
    for key, fn in (('fear_greed', fetch_fear_greed), ('valuation', fetch_valuation)):
        try:
            v = fn()
        except Exception as e:          # 보조 지표가 핵심 수집을 죽이지 않게
            print(f'[US] {key} 수집 예외: {e}')
            v = None
        if v:
            out[key] = v
    print(f'[US] 공포탐욕 {"OK" if "fear_greed" in out else "없음"} · 선행PER {"OK" if "valuation" in out else "없음"}')
    return out
