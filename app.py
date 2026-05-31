"""
ValueHunter — 퀀트 기반 가치분석 대시보드
Google Sheets(TODAY 시트)에서 뉴스를 읽어 종목별 탭으로 표시
"""

import re
import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timedelta
import streamlit.components.v1 as components
import pytz
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from utils.sheets import get_tickers, add_ticker, remove_ticker, reorder_tickers, get_today_news, get_sentiment
from utils.edgar import get_edgar_fundamentals
from utils.damodaran import enrich_fundamentals, INDUSTRY_CANDIDATES, INDUSTRY_OVERRIDE

# ── 프리미엄 게이트 ───────────────────────────────────────────────
# Streamlit Cloud 시크릿에서 PREMIUM_UNLOCKED=false 로 설정하면 잠금
# 현재 기본값: true (전체 공개)
PREMIUM_UNLOCKED = os.environ.get('PREMIUM_UNLOCKED', 'true').lower() == 'true'

KST = pytz.timezone('Asia/Seoul')
ET  = pytz.timezone('America/New_York')

KR_WEEKDAY = ['월', '화', '수', '목', '금', '토', '일']


def et_date_str():
    """ET + KST 날짜·시간 병기 문자열.
    예: 2026.05.15.(금) 14:32 ET  /  05.16.(토) 03:32 KST 기준
    """
    now_et  = datetime.now(ET)
    now_kst = now_et.astimezone(KST)
    wd_et  = KR_WEEKDAY[now_et.weekday()]
    wd_kst = KR_WEEKDAY[now_kst.weekday()]
    et_str  = now_et.strftime(f'%Y.%m.%d.({wd_et}) %H:%M ET')
    kst_str = now_kst.strftime(f'%m.%d.({wd_kst}) %H:%M KST')
    return f'{et_str}  /  {kst_str} 기준'


def format_summary_html(text):
    """
    summary_kr → 섹션별 HTML 변환.
    - 섹션 헤더 이전 종합 요약 텍스트 제거 (2a)
    - 섹션 헤더에 이모지·색상 적용 (2b)
    """
    if not text:
        return ''

    # 이모지 포함 헤더 매핑
    HEADER_MAP = {
        '[핵심 이슈]':   ('🔥', '핵심 이슈',   '#f38ba8'),
        '[투자 포인트]': ('💡', '투자 포인트', '#f9e2af'),
        '[시장 분위기]': ('📊', '시장 분위기', '#89b4fa'),
        # 이모지 이미 포함된 경우도 처리
        '[핵심 이슈] 🔥': ('🔥', '핵심 이슈',   '#f38ba8'),
        '[투자 포인트] 💡': ('💡', '투자 포인트', '#f9e2af'),
        '[시장 분위기] 📊': ('📊', '시장 분위기', '#89b4fa'),
    }
    SECTION_KEYS = set(HEADER_MAP.keys())

    text = re.sub(r'\n{3,}', '\n\n', text.strip())
    lines = text.split('\n')

    processed = []
    skip_empty = False
    found_first_header = False  # 첫 헤더 이전 텍스트 무시 (2a)

    for line in lines:
        stripped = line.strip()
        # 헤더 감지 (이모지 포함/미포함 모두)
        matched_key = None
        for k in SECTION_KEYS:
            if stripped.startswith(k) or stripped == k:
                matched_key = k
                break
        if matched_key:
            processed.append(('header', matched_key))
            skip_empty = True
            found_first_header = True
        elif not stripped:
            if not skip_empty and found_first_header:
                processed.append(('blank', ''))
        else:
            skip_empty = False
            if found_first_header:           # 헤더 이전 텍스트 무시 (2a)
                processed.append(('text', stripped))

    html_parts = []
    current_lines = []

    def flush_para():
        if current_lines:
            html_parts.append(
                '<p class="brief-para">' + '<br>'.join(current_lines) + '</p>'
            )
            current_lines.clear()

    for kind, val in processed:
        if kind == 'header':
            flush_para()
            emoji, label, color = HEADER_MAP.get(val, ('', val, '#cdd6f4'))
            html_parts.append(
                '<p class="brief-section" style="color:' + color + ';border-left:3px solid ' + color + ';">' +
                emoji + ' ' + label + '</p>'
            )
        elif kind == 'blank':
            flush_para()
        else:
            current_lines.append(val)

    flush_para()
    return ''.join(html_parts)


def lookup_company_name(ticker):
    """Yahoo Finance 비공개 API로 티커 → 회사명 조회. 실패 시 빈 문자열 반환."""
    try:
        url = (
            f"https://query1.finance.yahoo.com/v1/finance/search"
            f"?q={ticker}&quotesCount=5&newsCount=0&listsCount=0"
        )
        resp = requests.get(
            url,
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=5
        )
        data = resp.json()
        for q in data.get('quotes', []):
            if q.get('symbol', '').upper() == ticker.upper():
                return q.get('shortname') or q.get('longname') or ''
        return ''
    except Exception:
        return ''


@st.cache_data(ttl=3600)
def fetch_finnhub_data(ticker):
    """
    Finnhub 5개 엔드포인트 일괄 수집 (1시간 캐시).
    quote / metric / price-target / recommendation / earnings
    """
    api_key = os.environ.get('FINNHUB_API_KEY', '')
    if not api_key:
        return {}
    H = {'User-Agent': 'Mozilla/5.0'}
    BASE = 'https://finnhub.io/api/v1'
    data = {}
    try:
        # 1. 현재가 / 전일종가 / 등락
        r = requests.get(f'{BASE}/quote?symbol={ticker}&token={api_key}', headers=H, timeout=8)
        if r.ok:
            q = r.json()
            data.update({
                'prev_close': q.get('pc'), 'change': q.get('d'),
                'change_pct': q.get('dp'), 'current': q.get('c'),
                'quote_time': q.get('t'),  # unix timestamp (마지막 거래)
            })
        # 2. 펀더멘탈 지표 전체
        r = requests.get(f'{BASE}/stock/metric?symbol={ticker}&metric=all&token={api_key}', headers=H, timeout=8)
        if r.ok:
            m = r.json().get('metric', {})
            data.update({
                'pe':          m.get('peExclExtraTTM'),
                'roe':         m.get('roeTTM'),
                'eps':         m.get('epsExclExtraItemsTTM'),
                'div_yield':   m.get('dividendYieldIndicatedAnnual'),
                'beta':        m.get('beta'),
                'week52h':      m.get('52WeekHigh'),
                'week52l':      m.get('52WeekLow'),
                'week52h_date': m.get('52WeekHighDate'),
                'week52l_date': m.get('52WeekLowDate'),
                'mcap':        m.get('marketCapitalization'),
                'net_margin':  m.get('netProfitMarginTTM'),
                'gross_margin':m.get('grossMarginTTM'),
                'rev3y':       m.get('revenueGrowth3Y'),
                'rev5y':       m.get('revenueGrowth5Y'),
                'eps3y':       m.get('epsGrowth3Y'),
                'eps5y':       m.get('epsGrowth5Y'),
                'rev1y':       m.get('revenueGrowthTTMYoy'),
                'eps1y':       m.get('epsGrowthTTMYoy'),
            })
        # 3. 목표주가 (Finnhub → Yahoo Finance 폴백)
        r = requests.get(f'{BASE}/stock/price-target?symbol={ticker}&token={api_key}', headers=H, timeout=8)
        if r.ok:
            pt = r.json()
            data.update({
                'target_mean': pt.get('targetMean'),
                'target_high': pt.get('targetHigh'),
                'target_low':  pt.get('targetLow'),
            })
        # Finnhub 미제공 시 Yahoo Finance quoteSummary financialData 폴백
        if not data.get('target_mean'):
            _YF_H = {
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                'Accept': 'application/json,text/plain,*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://finance.yahoo.com/',
            }
            for _yhost in ('query2', 'query1'):
                try:
                    yurl = (f'https://{_yhost}.finance.yahoo.com/v10/finance/'
                            f'quoteSummary/{ticker}?modules=financialData&formatted=false')
                    yr = requests.get(yurl, headers=_YF_H, timeout=8)
                    if yr.ok:
                        fd = (yr.json().get('quoteSummary', {})
                                 .get('result', [{}])[0]
                                 .get('financialData', {}))
                        # formatted=false → 값이 직접 숫자로 옴
                        def _yf_raw(v):
                            if v is None: return None
                            return v.get('raw') if isinstance(v, dict) else v
                        tm = _yf_raw(fd.get('targetMeanPrice'))
                        tl = _yf_raw(fd.get('targetLowPrice'))
                        th = _yf_raw(fd.get('targetHighPrice'))
                        if tm:
                            data.update({'target_mean': tm,
                                         'target_low':  tl,
                                         'target_high': th})
                            break
                except Exception as _ye:
                    print(f'[YF price-target/{_yhost}] {_ye}')
        # 4. 애널리스트 투자의견
        r = requests.get(f'{BASE}/stock/recommendation?symbol={ticker}&token={api_key}', headers=H, timeout=8)
        if r.ok:
            recs = r.json()
            if recs:
                rec = recs[0]
                data.update({
                    'rec_sb': rec.get('strongBuy', 0),
                    'rec_b':  rec.get('buy', 0),
                    'rec_h':  rec.get('hold', 0),
                    'rec_s':  rec.get('sell', 0),
                    'rec_ss': rec.get('strongSell', 0),
                    'rec_period': rec.get('period', ''),
                })
        # 5. EPS 어닝 히스토리
        r = requests.get(f'{BASE}/stock/earnings?symbol={ticker}&token={api_key}', headers=H, timeout=8)
        if r.ok:
            data['earnings'] = (r.json() or [])[:4]
        # 6. 회사 프로필 (홈페이지 URL, 회사명, 로고 등)
        r = requests.get(f'{BASE}/stock/profile2?symbol={ticker}&token={api_key}', headers=H, timeout=8)
        if r.ok:
            p = r.json()
            data['company_weburl'] = p.get('weburl') or ''
            data['company_name']   = p.get('name') or ''
        # 7. prev day volume via daily candle
        try:
            import time as _t
            _to = int(_t.time())
            _fr = _to - 10 * 86400
            r = requests.get(
                f'{BASE}/stock/candle?symbol={ticker}&resolution=D'
                f'&from={_fr}&to={_to}&token={api_key}',
                headers=H, timeout=8)
            if r.ok:
                cv = r.json()
                if cv.get('s') == 'ok' and cv.get('v') and cv.get('t'):
                    # Use [-2] if last bar is today (intraday), else [-1]
                    from datetime import date as _date
                    last_date = _date.fromtimestamp(cv['t'][-1])
                    today = _date.today()
                    if last_date == today and len(cv['v']) >= 2:
                        data['prev_volume'] = int(cv['v'][-2])
                    else:
                        data['prev_volume'] = int(cv['v'][-1])
        except Exception:
            pass
        # 8. prev_volume YF fallback (Finnhub 실패 시)
        if data.get('prev_volume') is None:
            try:
                _yf_vol_url = (
                    f'https://query1.finance.yahoo.com/v8/finance/chart/'
                    f'{ticker}?interval=1d&range=5d'
                )
                _yr = requests.get(_yf_vol_url, headers=H, timeout=8)
                if _yr.ok:
                    _ch = _yr.json().get('chart', {}).get('result', [{}])[0]
                    _vols = (
                        _ch.get('indicators', {})
                           .get('quote', [{}])[0]
                           .get('volume', [])
                    )
                    _vols = [v for v in _vols if v is not None]
                    if len(_vols) >= 2:
                        data['prev_volume'] = int(_vols[-2])
                    elif _vols:
                        data['prev_volume'] = int(_vols[-1])
            except Exception:
                pass
    except Exception as e:
        print(f'[Finnhub] {ticker} 수집 오류: {e}')
    return data


@st.cache_data(ttl=86400)
def fetch_fear_greed():
    """
    Fear & Greed Index 수집 (24시간 캐시).
    1순위: CNN API  → production.dataviz.cnn.io
    2순위: feargreedmeter.com 스크래핑
    반환: {'score': int, 'rating': str, 'rating_kr': str, 'source': str} or None
    """
    H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    RATING_KR = {
        'Extreme Greed': '극단적 탐욕',
        'Greed':         '탐욕',
        'Neutral':       '중립',
        'Fear':          '공포',
        'Extreme Fear':  '극단적 공포',
    }
    def _rating_from_score(s):
        if s >= 75: return 'Extreme Greed'
        if s >= 55: return 'Greed'
        if s >= 45: return 'Neutral'
        if s >= 25: return 'Fear'
        return 'Extreme Fear'
    # 1. CNN API
    try:
        r = requests.get(
            'https://production.dataviz.cnn.io/index/fearandgreed/graphdata',
            headers=H, timeout=8
        )
        if r.ok:
            fg = r.json().get('fear_and_greed', {})
            score = fg.get('score')
            rating = fg.get('rating', '')
            if score is not None:
                score = round(float(score))
                if not rating:
                    rating = _rating_from_score(score)
                return {
                    'score': score, 'rating': rating,
                    'rating_kr': RATING_KR.get(rating, rating),
                    'source': 'CNN'
                }
    except Exception as e:
        print(f'[FearGreed] CNN 실패: {e}')
    # 2. feargreedmeter.com 폴백
    try:
        r = requests.get('https://feargreedmeter.com/', headers=H, timeout=10)
        if r.ok:
            import re as _re
            m = (_re.search(r'"(?:score|value)":\s*(\d{1,3})', r.text)
                 or _re.search(r'class="[^"]*(?:score|value|number)[^"]*"[^>]*>(\d{1,3})', r.text)
                 or _re.search(r'(?:Fear.*?Greed|Index)[^0-9]{0,30}(\d{1,3})', r.text, _re.IGNORECASE))
            if m:
                score = int(m.group(1))
                if 0 <= score <= 100:
                    rating = _rating_from_score(score)
                    return {
                        'score': score, 'rating': rating,
                        'rating_kr': RATING_KR.get(rating, rating),
                        'source': 'feargreedmeter.com'
                    }
    except Exception as e:
        print(f'[FearGreed] feargreedmeter 폴백 실패: {e}')
    return None


import datetime as _dt_ttl
_MACRO_TTL = 86400 if _dt_ttl.datetime.now().weekday() >= 5 else 3600

@st.cache_data(ttl=_MACRO_TTL)
def fetch_fred_data():
    """Fetch macro indicators from FRED API (평일 1h / 주말 24h cache)."""
    api_key = os.environ.get('FRED_API_KEY', '')
    if not api_key:
        return {}
    BASE = 'https://api.stlouisfed.org/fred/series/observations'
    H = {'User-Agent': 'Mozilla/5.0 (compatible; valuehunter/1.0)'}
    result = {}

    def _latest(sid):
        import time as _time
        for _attempt in range(3):
            try:
                r = requests.get(BASE, params={
                    'series_id': sid, 'api_key': api_key,
                    'file_type': 'json', 'sort_order': 'desc', 'limit': 30,
                }, headers=H, timeout=10)
                if r.ok:
                    for o in r.json().get('observations', []):
                        if o['value'] != '.':
                            return float(o['value']), o['date']
            except Exception as e:
                if _attempt == 2:
                    print(f'[FRED] {sid} 3회 실패: {e}')
                else:
                    _time.sleep(0.5)
        return None, None

    for key, sid in [('fed_rate', 'FEDFUNDS'), ('t10y', 'DGS10'),
                     ('t2y', 'DGS2'), ('credit_spread', 'BAA10Y'),
                     ('us_debt', 'GFDEBTN')]:
        v, d = _latest(sid)
        if v is not None:
            result[key] = {'value': v, 'date': d}

    # 원/달러 환율: Yahoo Finance USDKRW=X (실시간) → DEXKOUS(FRED) 폴백
    import time as _time_y
    for _kattempt in range(3):
        try:
            _kurl = ('https://query1.finance.yahoo.com/v8/finance/chart/'
                     'USDKRW%3DX?interval=1d&range=10d')
            _kr = requests.get(_kurl, headers=H, timeout=10)
            if _kr.ok:
                _kch = _kr.json().get('chart', {}).get('result', [{}])[0]
                _kc  = _kch.get('indicators', {}).get('quote', [{}])[0].get('close', [])
                _kt  = _kch.get('timestamp', [])
                _kc  = [c for c in _kc if c is not None]
                if _kc and _kt:
                    import datetime as _dt2
                    _kdate = _dt2.datetime.fromtimestamp(_kt[-1]).strftime('%Y-%m-%d')
                    result['krw_usd'] = {'value': round(_kc[-1], 2), 'date': _kdate}
                    break
        except Exception as _ke:
            if _kattempt == 2:
                print(f'[KRW Yahoo] 3회 실패: {_ke}')
            else:
                _time_y.sleep(0.5)
    # 폴백: FRED DEXKOUS (주 1회 업데이트)
    if 'krw_usd' not in result:
        _kv, _kd = _latest('DEXKOUS')
        if _kv is not None:
            result['krw_usd'] = {'value': _kv, 'date': _kd}

    if 't10y' in result and 't2y' in result:
        result['spread'] = round(
            result['t10y']['value'] - result['t2y']['value'], 2)

    # Buffett Indicator: Wilshire5000 / GDP * 100
    # FRED removed all Wilshire data June 2024 → use Yahoo Finance ^W5000
    w = None
    for _wattempt in range(3):
        try:
            yurl = ('https://query1.finance.yahoo.com/v8/finance/chart/'
                    '%5EW5000?interval=1d&range=10d')
            yr = requests.get(yurl, headers=H, timeout=10)
            if yr.ok:
                _ch = yr.json().get('chart', {}).get('result', [{}])[0]
                _closes = _ch.get('indicators', {}).get('quote', [{}])[0].get('close', [])
                _times  = _ch.get('timestamp', [])
                _closes = [c for c in _closes if c is not None]
                if _closes and _times:
                    w  = _closes[-1]
                    import datetime as _dt
                    wd = _dt.datetime.fromtimestamp(_times[-1]).strftime('%Y-%m-%d')
                    break
        except Exception as _e:
            if _wattempt == 2:
                print(f'[Buffett Yahoo] 3회 실패: {_e}')
            else:
                _time_y.sleep(0.5)
    g, _  = _latest('GDP')
    if w is not None and g is not None and g > 0:
        result['buffett'] = {'value': round(w / g * 100, 1), 'date': wd}

    return result


@st.cache_data(ttl=_MACRO_TTL)
def fetch_cape_data():
    """Shiller CAPE current + history from multpl.com (24h cache)."""
    import re as _re
    H = {'User-Agent': 'Mozilla/5.0 (compatible; valuehunter/1.0)'}
    current = None
    history = []
    try:
        r = requests.get('https://www.multpl.com/shiller-pe',
                         headers=H, timeout=10)
        if r.ok:
            # HTML: <div id='current'><b>...</b>\n41.66\n...
            m = _re.search(
                r'id=["\']current["\'][^>]*>[\s\S]*?</b>\s*([\d.]+)',
                r.text)
            if m:
                current = float(m.group(1))
    except Exception as e:
        print(f'[CAPE current] {e}')
    try:
        # by-month 사용: by-year는 3컬럼 구조로 연도값이 잘못 파싱됨
        r = requests.get(
            'https://www.multpl.com/shiller-pe/table/by-month',
            headers=H, timeout=15)
        if r.ok:
            # 태그 제거 방식으로 파싱 (regex보다 강건)
            seen = {}
            for row_raw in _re.split(r'</tr>', r.text, flags=_re.IGNORECASE):
                cells = _re.findall(
                    r'<td[^>]*>([\s\S]*?)</td>', row_raw, _re.IGNORECASE)
                if len(cells) < 2:
                    continue
                # 태그 제거 후 순수 텍스트 추출
                date_text = _re.sub(r'<[^>]+>', '', cells[0]).strip()
                val_text  = _re.sub(r'<[^>]+>', '', cells[1]).strip()
                # 날짜 셀: 월 이름으로 시작해야 함
                if not _re.match(
                    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)',
                    date_text):
                    continue
                yr_m = _re.search(r'\d{4}', date_text)
                if not yr_m:
                    continue
                try:
                    val = float(val_text.replace(',', ''))
                    if not (1.0 <= val <= 100.0):  # CAPE 유효 범위 (연도값 제외)
                        continue
                    label = yr_m.group()
                    if label not in seen:           # 연도당 최신 월 1개
                        seen[label] = val
                except (ValueError, TypeError):
                    pass
            history = sorted(seen.items(), key=lambda x: x[0])
    except Exception as e:
        print(f'[CAPE history] {e}')
    # 최신 월 날짜 추출 (history 첫 번째 항목 기반)
    cape_date = ''
    if history:
        try:
            import datetime as _cdt
            latest_yr = history[-1][0]  # history는 연도 오름차순
            cape_date = f'{latest_yr}-01'
        except Exception:
            pass
    return {'current': current, 'history': history, 'date': cape_date}


@st.cache_data(ttl=_MACRO_TTL)
def fetch_buffett_history():
    """버핏지수 역사 데이터: Yahoo Finance ^W5000 분기 + FRED GDP 분기 병합 (24h cache)."""
    import os as _os, datetime as _dt
    H = {'User-Agent': 'Mozilla/5.0 (compatible; valuehunter/1.0)'}
    w_data = {}
    try:
        wurl = ('https://query1.finance.yahoo.com/v8/finance/chart/'
                '%5EW5000?interval=3mo&range=max')
        wr = requests.get(wurl, headers=H, timeout=20)
        if wr.ok:
            _wch = wr.json().get('chart', {}).get('result', [{}])[0]
            _wt  = _wch.get('timestamp', [])
            _wc  = _wch.get('indicators', {}).get('quote', [{}])[0].get('close', [])
            for t, c in zip(_wt, _wc):
                if c is not None:
                    d = _dt.datetime.fromtimestamp(t)
                    w_data[f"{d.year}Q{(d.month-1)//3+1}"] = c
    except Exception as _we:
        print(f'[BuffettHist W5000] {_we}')
    g_data = {}
    _api = _os.environ.get('FRED_API_KEY', '')
    if _api:
        try:
            _gr = requests.get(
                'https://api.stlouisfed.org/fred/series/observations',
                params={'series_id': 'GDP', 'api_key': _api,
                        'file_type': 'json', 'observation_start': '1971-01-01',
                        'sort_order': 'asc', 'limit': 1000},
                headers=H, timeout=15)
            if _gr.ok:
                for _o in _gr.json().get('observations', []):
                    if _o['value'] != '.':
                        _d = _o['date']
                        _y, _m = int(_d[:4]), int(_d[5:7])
                        g_data[f"{_y}Q{(_m-1)//3+1}"] = float(_o['value'])
        except Exception as _ge:
            print(f'[BuffettHist GDP] {_ge}')
    result = []
    for key in sorted(set(w_data) & set(g_data)):
        g = g_data[key]
        if g > 0:
            result.append((key, round(w_data[key] / g * 100, 1)))
    return result



def _macro_row(label, value_str, color='#cdd6f4', sub='', tip=''):
    sub_html = (
        f'<span style="font-size:0.6rem;color:#585b70;margin-left:4px;">'
        f'{sub}</span>' if sub else '')
    tip_html = ''
    if tip:
        tip_html = (
            f'<span class="macro-tip">💡'
            f'<span class="tip-box">{tip}</span></span>')
    return (
        f'<div style="display:flex;justify-content:space-between;'
        f'align-items:center;padding:3px 0;border-bottom:1px solid #181825;">'
        f'<span style="font-size:0.7rem;color:#a6adc8;">{label}{tip_html}</span>'
        f'<span style="font-size:0.78rem;font-weight:600;color:{color};">'
        f'{value_str}{sub_html}</span></div>'
    )


def render_premium_lock(icon, title, desc):
    """프리미엄 잠금 플레이스홀더 카드."""
    st.markdown(
        f'<div class="premium-lock-card">'
        f'<div class="premium-lock-icon">{icon}</div>'
        f'<div>'
        f'<div class="premium-lock-title">{title}</div>'
        f'<div class="premium-lock-desc">{desc}</div>'
        f'</div>'
        f'<div class="premium-lock-badge">🔒 구독 전용</div>'
        f'</div>',
        unsafe_allow_html=True
    )


def render_stock_header(ticker_sym, data, fundamentals=None):
    """
    안 A: 탭 상단 가격 4칩 + 핵심지표 6칩 (항상 표시)
    안 B: 성장률·애널리스트·어닝 히스토리 expander (접기/펼치기)
    """
    api_key = os.environ.get('FINNHUB_API_KEY', '')
    if not api_key:
        st.caption('⚠️ FINNHUB_API_KEY 시크릿이 설정되지 않았습니다.')
        return
    if not data:
        st.caption('⚠️ Finnhub API 응답 없음 — 새로고침을 눌러주세요.')
        return

    def _v(val, fmt='.2f', pref='', suf='', na='—'):
        if val is None:
            return na
        try:
            return f'{pref}{val:{fmt}}{suf}'
        except Exception:
            return na

    def _fmt_mcap(val, krw_rate=None):
        if val is None:
            return '—'
        usd_str = (
            f'${val / 1_000_000:.2f}T' if val >= 1_000_000 else
            f'${val / 1_000:.0f}B'    if val >= 1_000 else
            f'${val:.0f}M'
        )
        if krw_rate:
            krw_jo = val * 1_000_000 * krw_rate / 1e12
            if krw_jo >= 1:
                usd_str += f' (₩{krw_jo:,.0f}조)'
            else:
                krw_eok = val * 1_000_000 * krw_rate / 1e8
                usd_str += f' (₩{krw_eok:,.0f}억)'
        return usd_str

    with st.expander('📈 주가정보', expanded=False):
        # ── 홈페이지 링크 ────────────────────────────────────────────
        weburl = (data.get('company_weburl') or '').strip()
        cname  = (data.get('company_name') or '').strip()
        if weburl:
            display_url = weburl.rstrip('/').replace('https://', '').replace('http://', '')
            link_label  = cname if cname else display_url
            st.markdown(
                f'<div style="margin-bottom:10px;">'
                f'<a href="{weburl}" target="_blank" rel="noopener noreferrer" '
                f'style="font-size:0.78rem;color:#89b4fa;text-decoration:none;display:inline-flex;align-items:center;gap:4px;">'
                f'🌐 <span>{link_label}</span>'
                f'<span style="font-size:0.65rem;opacity:0.6;">↗</span>'
                f'</a>'
                f'<span style="font-size:0.7rem;color:#585b70;margin-left:6px;">{display_url}</span>'
                f'</div>',
                unsafe_allow_html=True
            )
        # ── 안 A: 가격 4칩 ──────────────────────────────────────────
        chg_pct = data.get('change_pct')
        chg_cls = 'up' if (chg_pct or 0) > 0 else ('down' if (chg_pct or 0) < 0 else '')
        chg_sign = '+' if (chg_pct or 0) > 0 else ''
        chg_str  = f'{chg_sign}{chg_pct:.2f}%' if chg_pct is not None else '—'

        pc   = _v(data.get('prev_close'), '.2f', '$')
        h52  = _v(data.get('week52h'),    '.2f', '$')
        l52  = _v(data.get('week52l'),    '.2f', '$')
        def _w52_date(raw):
            """Finnhub 52WeekHighDate/LowDate → 'MM/DD' 형식 또는 ''"""
            if not raw: return ''
            try:
                # 형식 예: '2024-05-01' or '2024-05-01 00:00:00'
                d = str(raw)[:10]
                from datetime import datetime as _dt
                dt = _dt.strptime(d, '%Y-%m-%d')
                return dt.strftime('%y/%m/%d')
            except Exception:
                return str(raw)[:10]
        h52_date = _w52_date(data.get('week52h_date'))
        l52_date = _w52_date(data.get('week52l_date'))
        h52_date_span = (f' <span class="fc-date">{h52_date}</span>' if h52_date else '')
        l52_date_span = (f' <span class="fc-date">{l52_date}</span>' if l52_date else '')
        mcap = _fmt_mcap(data.get('mcap'))
        def _pct_vs(curr, ref):
            if curr is None or ref is None or ref == 0: return '—', ''
            p = (curr - ref) / ref * 100
            return f'{p:+.1f}%', ('up' if p > 0 else 'down' if p < 0 else '')
        _cp = data.get('current')
        h52_chg, h52_cls = _pct_vs(_cp, data.get('week52h'))
        l52_chg, l52_cls = _pct_vs(_cp, data.get('week52l'))
        vol_v = data.get('prev_volume')
        if vol_v is not None:
            if vol_v >= 1_000_000: vol_str = f'{vol_v/1_000_000:.1f}M'
            elif vol_v >= 1_000:   vol_str = f'{vol_v/1_000:.0f}K'
            else:                  vol_str = f'{vol_v:,}'
        else:
            vol_str = '—'

        # 기준일시 (ET 장 종료 기준)
        qt = data.get('quote_time')
        qt_str = ''
        if qt:
            try:
                qt_dt  = datetime.fromtimestamp(qt, tz=ET)
                wd     = KR_WEEKDAY[qt_dt.weekday()]
                qt_str = qt_dt.strftime(f'%m/%d({wd}) %H:%M ET')
            except Exception:
                qt_str = ''

        qt_span = ('<span class="fc-date">' + qt_str + '</span>') if qt_str else ''
        st.markdown(
            f'<div class="fin-grid">'
            f'<div class="fin-chip"><div class="fc-label">전일 종가 {qt_span}</div>'
            f'<div class="fc-value">{pc}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">등락률</div><div class="fc-value {chg_cls}">{chg_str}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">52주 최고가{h52_date_span}</div><div class="fc-value">{h52}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">52주 고 대비</div><div class="fc-value {h52_cls}">{h52_chg}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">52주 최저가{l52_date_span}</div><div class="fc-value">{l52}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">52주 저 대비</div><div class="fc-value {l52_cls}">{l52_chg}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">전일 거래량</div><div class="fc-value">{vol_str}</div></div>'
            f'</div>',
            unsafe_allow_html=True
        )

        # ── 안 A: 핵심지표 6칩 ──────────────────────────────────────
        pe      = _v(data.get('pe'),        '.1f', suf='x')
        roe_v   = data.get('roe')
        roe     = f'{roe_v:.1f}%' if roe_v is not None else '—'
        eps       = _v(data.get('eps'),        '.2f', '$')
        t_mean_v  = data.get('target_mean')
        t_low_v   = data.get('target_low')
        t_high_v  = data.get('target_high')
        t_mean    = _v(t_mean_v, '.1f', '$')
        t_low     = _v(t_low_v,  '.1f', '$')
        t_high    = _v(t_high_v, '.1f', '$')
        div_v   = data.get('div_yield')
        div     = f'{div_v:.2f}%' if div_v is not None else '—'
        beta    = _v(data.get('beta'),      '.2f')

        # 업종 베타 + 업종명 (fundamentals에서)
        ind_beta_val = (fundamentals.get('industry_beta') if fundamentals else None)
        ind_name_s   = (fundamentals.get('damod_industry') or '') if fundamentals else ''
        beta_label   = '베타'
        if ind_beta_val is not None:
            beta_label = f'베타  <span class="fc-date">업종 {round(ind_beta_val,2)} · {ind_name_s}</span>'

        st.markdown(
            f'<div class="fin-grid">'
            f'<div class="fin-chip"><div class="fc-label">PER (TTM)</div><div class="fc-value">{pe}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">ROE</div><div class="fc-value">{roe}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">EPS (TTM)</div><div class="fc-value">{eps}</div></div>'
            f'<div class="fin-chip" style="min-width:170px;flex:2;">'
            f'<div class="fc-label">목표주가</div>'
            f'<div style="display:flex;gap:6px;align-items:baseline;flex-wrap:wrap;margin-top:3px;">'
            f'<span style="font-size:0.68rem;color:#7f849c;">최저</span>'
            f'<span style="font-size:0.85rem;font-weight:600;color:#a6e3a1;">{t_low}</span>'
            f'<span style="color:#45475a;font-size:0.7rem;">·</span>'
            f'<span style="font-size:0.68rem;color:#7f849c;">평균</span>'
            f'<span style="font-size:0.85rem;font-weight:600;color:#89dceb;">{t_mean}</span>'
            f'<span style="color:#45475a;font-size:0.7rem;">·</span>'
            f'<span style="font-size:0.68rem;color:#7f849c;">최고</span>'
            f'<span style="font-size:0.85rem;font-weight:600;color:#fab387;">{t_high}</span>'
            f'</div></div>'
            f'<div class="fin-chip"><div class="fc-label">배당수익률</div><div class="fc-value">{div}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">{beta_label}</div><div class="fc-value">{beta}</div></div>'
            f'</div>',
            unsafe_allow_html=True
        )

        # ── 기업 정보 칩 (발행주식수·시총 / 향후: 주요주주·CEO 등) ──
        company_chips = []
        shares = (fundamentals.get('shares_outstanding') if fundamentals else None)
        mcap_v = data.get('mcap')
        if shares is not None:
            sh_str = f'{shares:,.0f}주'
            company_chips.append(('발행주식수', sh_str))
        if mcap_v is not None:
            try:
                _fred_d = fetch_fred_data()
                _krw_r  = (_fred_d.get('krw_usd', {}).get('value')
                           if _fred_d else None)
            except Exception:
                _krw_r = None
            company_chips.append(('시가총액', _fmt_mcap(mcap_v, _krw_r)))
        # 향후 확장 예시: company_chips.append(('주요주주', '...'))
        if company_chips:
            chips_html = ''.join(
                f'<div class="fin-chip"><div class="fc-label">{lbl}</div>'
                f'<div class="fc-value">{val}</div></div>'
                for lbl, val in company_chips
            )
            st.markdown(f'<div class="fin-grid">{chips_html}</div>', unsafe_allow_html=True)

        # ── 안 B: 상세 지표 expander ────────────────────────────────
        st.markdown('<hr style="border:none;border-top:1px solid #313244;margin:14px 0 6px 0">', unsafe_allow_html=True)
        st.markdown('<div class="expander-section-label" style="margin-top:4px">📊 상세 지표</div>', unsafe_allow_html=True)

        # 성장률·수익성
        r1y = _v(data.get('rev1y'),       '.1f', suf='%')
        r3y = _v(data.get('rev3y'),       '.1f', suf='%')
        r5y = _v(data.get('rev5y'),       '.1f', suf='%')
        e1y = _v(data.get('eps1y'),       '.1f', suf='%')
        e3y = _v(data.get('eps3y'),       '.1f', suf='%')
        e5y = _v(data.get('eps5y'),       '.1f', suf='%')
        nm  = _v(data.get('net_margin'),  '.1f', suf='%')
        gm  = _v(data.get('gross_margin'),'.1f', suf='%')

        st.markdown(
            '<div class="expander-section-label">성장률 · 수익성</div>'
            f'<div class="fin-grid">'
            f'<div class="fin-chip"><div class="fc-label">매출성장 1Y (TTM)</div><div class="fc-value">{r1y}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">매출성장 3Y</div><div class="fc-value">{r3y}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">매출성장 5Y</div><div class="fc-value">{r5y}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">EPS성장 1Y (TTM)</div><div class="fc-value">{e1y}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">EPS성장 3Y</div><div class="fc-value">{e3y}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">EPS성장 5Y</div><div class="fc-value">{e5y}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">순이익률 (TTM)</div><div class="fc-value">{nm}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">매출총이익률 (TTM)</div><div class="fc-value">{gm}</div></div>'
            f'</div>',
            unsafe_allow_html=True
        )

        # 애널리스트 투자의견
        sb = int(data.get('rec_sb') or 0)
        b  = int(data.get('rec_b')  or 0)
        h  = int(data.get('rec_h')  or 0)
        s  = int(data.get('rec_s')  or 0)
        ss = int(data.get('rec_ss') or 0)
        total_rec = sb + b + h + s + ss
        period = data.get('rec_period', '')

        if total_rec > 0:
            def _pw(n):
                return f'{n / total_rec * 100:.0f}%'
            t_high = _v(data.get('target_high'), '.1f', '$')
            t_low  = _v(data.get('target_low'),  '.1f', '$')
            t_mean = _v(data.get('target_mean'), '.1f', '$')
            per_label = f' ({period})' if period else ''
            st.markdown(
                f'<div class="expander-section-label">애널리스트 투자의견{per_label}</div>'
                f'<div class="rec-bar-wrap">'
                f'<div class="rec-segment" style="width:{_pw(sb)};background:#a6e3a1;"></div>'
                f'<div class="rec-segment" style="width:{_pw(b)};background:#94e2d5;"></div>'
                f'<div class="rec-segment" style="width:{_pw(h)};background:#585b70;"></div>'
                f'<div class="rec-segment" style="width:{_pw(s)};background:#fab387;"></div>'
                f'<div class="rec-segment" style="width:{_pw(ss)};background:#f38ba8;"></div>'
                f'</div>'
                f'<div class="rec-labels">'
                f'<span class="rec-label"><span class="rec-dot" style="background:#a6e3a1;"></span>강력매수 {sb}</span>'
                f'<span class="rec-label"><span class="rec-dot" style="background:#94e2d5;"></span>매수 {b}</span>'
                f'<span class="rec-label"><span class="rec-dot" style="background:#585b70;"></span>중립 {h}</span>'
                f'<span class="rec-label"><span class="rec-dot" style="background:#fab387;"></span>매도 {s}</span>'
                f'<span class="rec-label"><span class="rec-dot" style="background:#f38ba8;"></span>강력매도 {ss}</span>'
                f'</div>'
                f'<div style="margin-top:10px;font-size:0.8rem;color:#a6adc8;">'
                f'목표주가 &nbsp;최저 <b>{t_low}</b>&nbsp;·&nbsp;'
                f'평균 <b style="color:#89dceb">{t_mean}</b>&nbsp;·&nbsp;'
                f'최고 <b>{t_high}</b>'
                f'</div>',
                unsafe_allow_html=True
            )

        # EPS 어닝 히스토리
        earnings = data.get('earnings') or []
        if earnings:
            rows_html = ''
            for e in earnings:
                prd    = e.get('period', '')
                actual = e.get('actual')
                est    = e.get('estimate')
                surp   = e.get('surprisePercent')
                act_s  = f'${actual:.2f}' if actual is not None else '—'
                est_s  = f'${est:.2f}'    if est    is not None else '—'
                if surp is not None:
                    sign = '+' if surp > 0 else ''
                    cls  = 'earn-beat' if surp > 0 else 'earn-miss'
                    surp_s = f'<span class="{cls}">{sign}{surp:.1f}%</span>'
                else:
                    surp_s = '—'
                rows_html += (
                    f'<tr><td>{prd}</td><td>{act_s}</td>'
                    f'<td>{est_s}</td><td>{surp_s}</td></tr>'
                )
            st.markdown(
                '<div class="expander-section-label">EPS 어닝 히스토리 (최근 4분기)</div>'
                f'<table class="earn-table">'
                f'<thead><tr>'
                f'<th style="text-align:left">분기</th>'
                f'<th>실제 EPS</th><th>예상 EPS</th><th>서프라이즈</th>'
                f'</tr></thead>'
                f'<tbody>{rows_html}</tbody>'
                f'</table>',
                unsafe_allow_html=True
            )


def render_premium_analysis(ticker_sym, fundamentals=None):
    ss_key = 'ind_override_' + ticker_sym
    if ss_key not in st.session_state:
        st.session_state[ss_key] = ''

    def _ind_changed():
        try:
            fetch_premium_fundamentals.clear()
        except Exception:
            pass

    with st.expander('🔬 가치평가', expanded=False):

        # ── 업종 배지 + 기업 유형 배지 + 업종 선택기 ────────────────
        if PREMIUM_UNLOCKED and fundamentals:
            damod_ind  = fundamentals.get('damod_industry', '') or ''
            ind_src    = fundamentals.get('industry_source', 'finnhub_auto')
            is_ov      = ind_src in ('override_user', 'override_auto')
            badge_cls  = 'industry-badge overridden' if is_ov else 'industry-badge'
            src_lbl    = ('🔄 수동' if ind_src == 'override_user'
                          else '⚙️ 자동보정' if ind_src == 'override_auto'
                          else '🤖 자동')
            badge_txt  = damod_ind if damod_ind else '업종 미매핑'
            is_conglom = fundamentals.get('is_conglomerate', False)
            is_hg_top  = fundamentals.get('is_high_growth', False)
            type_badges = ''
            if is_conglom:
                type_badges += '<span class="entity-badge conglom">🔀 복합 기업</span>'
            if is_hg_top:
                type_badges += ' <span class="entity-badge highgrowth">🚀 고성장 기업</span>'
            st.markdown(
                '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;">'
                + '<div class="' + badge_cls + '">🏷️ ' + src_lbl + ': ' + badge_txt + '</div>'
                + type_badges + '</div>',
                unsafe_allow_html=True
            )
            candidates = INDUSTRY_CANDIDATES.get(ticker_sym.upper(), [])
            if candidates:
                options   = [''] + [lbl for lbl, _ in candidates]
                val_map   = {lbl: d for lbl, d in candidates}
                cur_sel   = st.session_state[ss_key]
                cur_label = next((lbl for lbl, d in candidates if d == cur_sel), '')
                cur_idx   = options.index(cur_label) if cur_label in options else 0
                sel_label = st.selectbox(
                    '업종 직접 선택 (결과값 변경)',
                    options=options,
                    index=cur_idx,
                    format_func=lambda x: '🔍 자동감지 사용' if x == '' else x,
                    key='sel_' + ticker_sym,
                    on_change=_ind_changed,
                    help='업종 선택에 따라 WACC, EV/EBITDA, ROIC 기준값이 달라집니다'
                )
                new_ov = val_map.get(sel_label, '')
                if new_ov != st.session_state[ss_key]:
                    st.session_state[ss_key] = new_ov
                    st.rerun()

        # ── 공통 헬퍼 ────────────────────────────────────────────────
        def _ind_tag(ind):
            if not ind:
                return ''
            return (' <span style="font-size:0.72rem;font-weight:400;color:#a6adc8;background:#313244;'
                    'border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:middle;">🏷️ '
                    + ind + '</span>')

        def _warns(msgs):
            if not msgs:
                return ''
            return ''.join('<div class="analysis-warn">⚠️ ' + m + '</div>' for m in msgs)

        damod_ind  = (fundamentals.get('damod_industry') or '') if fundamentals else ''
        is_conglom = fundamentals.get('is_conglomerate', False) if fundamentals else False
        is_hg      = fundamentals.get('is_high_growth', False)  if fundamentals else False

        # ① ROIC vs WACC ─────────────────────────────────────────────
        if PREMIUM_UNLOCKED:
            roic_val   = fundamentals.get('roic')             if fundamentals else None
            wacc_val   = fundamentals.get('wacc_used')        if fundamentals else None
            spread_val = fundamentals.get('roic_wacc_spread') if fundamentals else None
            ind_roic   = fundamentals.get('industry_roic')    if fundamentals else None
            if roic_val is not None and wacc_val is not None:
                spread  = spread_val if spread_val is not None else (roic_val - wacc_val)
                sp_cls  = 'positive' if spread > 0 else 'negative'
                sp_sign = '+' if spread > 0 else ''
                v_cls   = 'verdict-buy' if spread > 5 else ('verdict-watch' if spread > 0 else 'verdict-pass')
                v_txt   = '✅ 가치 창출 (EVA 양수)' if spread > 0 else '⚠️ 자본 파괴 (EVA 음수)'
                ind_c   = ('<div class="analysis-chip"><div class="chip-label">업종 ROIC</div>'
                           '<div class="chip-value">' + str(round(ind_roic, 1)) + '%</div></div>') if ind_roic is not None else ''
                wlist = []
                if is_conglom:
                    wlist.append('복합 기업: 단일 업종 WACC 적용 시 신뢰도 저하. 사업부별 가중 WACC가 이상적.')
                if is_hg:
                    wlist.append('고성장 기업: ROIC 낮더라도 매출성장·재투자 효율로 미래 가치 창출 가능.')
                ind_roic_cmp = ''
                if ind_roic is not None:
                    roic_gap = round(roic_val - ind_roic, 1)
                    if roic_gap >= 0:
                        ind_roic_cmp = (' 업종 평균 ROIC ' + str(round(ind_roic, 1)) + '%와 비교 시 '
                                        + str(roic_gap) + '%p 상회 — 동종사 대비 우위.')
                    else:
                        ind_roic_cmp = (' 업종 평균 ROIC ' + str(round(ind_roic, 1)) + '%와 비교 시 '
                                        + str(abs(roic_gap)) + '%p 갭 — 추가 개선 여지.')
                hint = ('ROIC ' + str(round(roic_val, 1)) + '%, WACC ' + str(round(wacc_val, 1)) + '% → 스프레드 '
                        + sp_sign + str(round(spread, 1)) + '%p. '
                        + ('자본 비용을 초과하는 수익을 내고 있어 경제적 해자 존재.' if spread > 0
                           else 'WACC 미달. 업종 분류 오류 가능성 — 위 선택기로 변경 후 재확인 권장.')
                        + ind_roic_cmp)
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">① 퀄리티 필터 — ROIC vs WACC' + _ind_tag(damod_ind) + '</div>'
                    '<div class="analysis-card-subtitle">투하자본이익률(ROIC)이 자본조달비용(WACC)을 초과하면 실질 가치 창출 기업</div>'
                    '<div class="analysis-metric-row">'
                    '<div class="analysis-chip"><div class="chip-label">ROIC</div>'
                    '<div class="chip-value">' + str(round(roic_val, 1)) + '%</div></div>'
                    '<div class="analysis-chip"><div class="chip-label">WACC (재레버링)</div>'
                    '<div class="chip-value">' + str(round(wacc_val, 1)) + '%</div></div>'
                    '<div class="analysis-chip"><div class="chip-label">스프레드</div>'
                    '<div class="chip-value ' + sp_cls + '">' + sp_sign + str(round(spread, 1)) + '%p</div></div>'
                    + ind_c + '</div>'
                    + _warns(wlist)
                    + '<div class="analysis-verdict ' + v_cls + '">' + v_txt + '</div>'
                    '<details class="analysis-hint-details" open><summary>▶ 해석 보기</summary>'
                    '<div class="analysis-hint">' + hint + '</div></details>'
                    '</div>', unsafe_allow_html=True
                )
            else:
                w = '⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다'
                if fundamentals and fundamentals.get('error'):
                    w = '⚠️ ' + str(fundamentals['error'])
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">① 퀄리티 필터 — ROIC vs WACC</div>'
                    '<div class="analysis-card-subtitle">투하자본이익률(ROIC)이 자본조달비용(WACC)을 초과하면 실질 가치 창출 기업</div>'
                    '<div class="analysis-verdict verdict-wait">' + w + '</div></div>', unsafe_allow_html=True)
        else:
            render_premium_lock('📊', '퀄리티 필터 — ROIC vs WACC 분석',
                '투하자본이익률(ROIC)과 가중평균자본비용(WACC)을 비교해 실질적 가치 창출 기업을 선별합니다.')

        # ② EV/EBITDA ─────────────────────────────────────────────────
        if PREMIUM_UNLOCKED:
            ev_eb    = fundamentals.get('ev_ebitda')          if fundamentals else None
            ind_ev   = fundamentals.get('industry_ev_ebitda') if fundamentals else None
            ebitda_v = fundamentals.get('ebitda')             if fundamentals else None
            if ev_eb is not None:
                disc  = ((ind_ev - ev_eb) / ind_ev * 100) if ind_ev else None
                d_cls = 'positive' if (disc or 0) > 0 else 'negative'
                v_cls = ('verdict-buy' if (disc or 0) > 20 else
                         'verdict-watch' if (disc or 0) > 0 else
                         'verdict-pass') if disc is not None else 'verdict-watch'
                v_txt = ('✅ 업종 대비 ' + str(round(disc)) + '% 할인' if (disc or 0) > 0
                         else '⚠️ 업종 대비 ' + str(round(-(disc or 0))) + '% 프리미엄') if disc is not None else '업종 EV/EBITDA 매핑 불가'
                disc_str = ((' → 업종 대비 ' + str(round(disc)) + '% 할인.') if (disc or 0) > 0
                              else (' → 업종 대비 ' + str(round(abs(disc or 0))) + '% 프리미엄.') if disc is not None
                              else '.')
                hint  = ('현재 EV/EBITDA ' + str(round(ev_eb, 1)) + 'x'
                         + (', 업종 중앙값 ' + str(round(ind_ev, 1)) + 'x' if ind_ev else '')
                         + disc_str + ' '
                         + ('이익 성장 시 밸류에이션 정상화 기대.' if (disc or 0) > 0
                            else '미래 성장 프리미엄 반영. 성장 둔화 시 멀티플 압축 리스크.' if disc is not None
                            else '업종 중앙값 없음. 절대 배수(10~20x)와 직접 비교 권장.'))
                i_c   = ('<div class="analysis-chip"><div class="chip-label">업종 중앙값</div>'
                         '<div class="chip-value">' + str(round(ind_ev, 1)) + 'x</div></div>') if ind_ev else ''
                d_c   = ('<div class="analysis-chip"><div class="chip-label">할인율</div>'
                         '<div class="chip-value ' + d_cls + '">'
                         + ('+' if (disc or 0) > 0 else '') + str(round(disc or 0)) + '%</div></div>') if disc is not None else ''
                wlist = []
                if is_conglom:
                    wlist.append('복합 기업: 단일 업종 멀티플 적용 한계. 사업부별 SOTP 분석이 더 적합.')
                if is_hg and ebitda_v is not None and ebitda_v < 0:
                    wlist.append('EBITDA 음수: EV/EBITDA 적용 불가. ⑥ PSR 카드로 대체 평가 권장.')
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">② 밸류 필터 — EV/EBITDA 상대 배수' + _ind_tag(damod_ind) + '</div>'
                    '<div class="analysis-card-subtitle">기업 전체가치(EV)를 영업현금흐름(EBITDA)으로 나눈 배수 — 업종 평균 대비 할인/프리미엄 확인</div>'
                    '<div class="analysis-metric-row">'
                    '<div class="analysis-chip"><div class="chip-label">EV/EBITDA</div>'
                    '<div class="chip-value">' + str(round(ev_eb, 1)) + 'x</div></div>'
                    + i_c + d_c + '</div>'
                    + _warns(wlist)
                    + '<div class="analysis-verdict ' + v_cls + '">' + v_txt + '</div>'
                    '<details class="analysis-hint-details" open><summary>▶ 해석 보기</summary>'
                    '<div class="analysis-hint">' + hint + '</div></details>'
                    '</div>', unsafe_allow_html=True
                )
            else:
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">② 밸류 필터 — EV/EBITDA 상대 배수</div>'
                    '<div class="analysis-card-subtitle">기업 전체가치(EV)를 영업현금흐름(EBITDA)으로 나눈 배수 — 업종 평균 대비 할인/프리미엄 확인</div>'
                    '<div class="analysis-verdict verdict-wait">⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다</div></div>',
                    unsafe_allow_html=True)
        else:
            render_premium_lock('💹', '밸류 필터 — EV/EBITDA 업종 대비 분석',
                '개별 종목 EV/EBITDA를 다모다란 업종 중앙값과 비교해 저평가 여부를 정량적으로 판단합니다.')

        # ③ DCF ──────────────────────────────────────────────────────
        if PREMIUM_UNLOCKED:
            dcf_iv = fundamentals.get('dcf_value')     if fundamentals else None
            price  = fundamentals.get('current_price') if fundamentals else None
            wacc_u = fundamentals.get('wacc_used')     if fundamentals else None
            fcf_v  = fundamentals.get('fcf')           if fundamentals else None
            if dcf_iv is not None and dcf_iv > 0:
                margin = ((dcf_iv - price) / dcf_iv * 100) if price else None
                m_cls  = 'positive' if (margin or 0) > 0 else 'negative'
                v_cls  = ('verdict-buy' if (margin or 0) > 30 else
                          'verdict-watch' if (margin or 0) > 0 else 'verdict-pass') if margin is not None else 'verdict-wait'
                v_txt  = ('✅ 안전마진 ' + str(round(margin or 0)) + '% — 저평가 신호' if (margin or 0) > 30 else
                          '✅ 내재가치 대비 ' + str(round(margin or 0)) + '% 여유' if (margin or 0) > 0 else
                          '⚠️ 현재가 내재가치 ' + str(round(-(margin or 0))) + '% 초과') if margin is not None else '주가 데이터 확인 중'
                wlist  = []
                if is_hg and (fcf_v is None or (fcf_v or 0) <= 0):
                    wlist.append('고성장 기업: FCF 음수 구간에서 DCF 신뢰도 낮음. ⑤ Scenario DCF 병행 확인 권장.')
                elif is_hg:
                    wlist.append('고성장 기업: 단일 성장률 가정 DCF는 보수적 추정치. ⑤ Scenario DCF 참고 권장.')
                hint   = ('내재가치 $' + str(round(dcf_iv, 2))
                          + (', 현재 주가 $' + str(round(price, 2)) if price else '')
                          + ((' → 안전마진 ' + ('+' if (margin or 0) > 0 else '') + str(round(margin or 0)) + '%. ') if margin is not None else '. ')
                          + ('안전마진 30%+ — 가치투자 기준 충족.' if (margin or 0) > 30
                             else '현재 주가 기준 저평가 구간. 성장 기대치와 종합 판단 권장.' if (margin or 0) > 0
                             else '현재가가 내재가치 초과 — 고성장 기업에서는 일반적.')
                          + ' 단, FCF 흑자 기업에만 유효한 모델.')
                p_c = ('<div class="analysis-chip"><div class="chip-label">현재가</div>'
                       '<div class="chip-value">$' + str(round(price, 2)) + '</div></div>') if price else ''
                m_c = ('<div class="analysis-chip"><div class="chip-label">안전마진</div>'
                       '<div class="chip-value ' + m_cls + '">'
                       + ('+' if (margin or 0) > 0 else '') + str(round(margin or 0)) + '%</div></div>') if margin is not None else ''
                w_c = ('<div class="analysis-chip"><div class="chip-label">할인율(WACC)</div>'
                       '<div class="chip-value">' + str(round(wacc_u, 1)) + '%</div></div>') if wacc_u else ''
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">③ DCF 보조 검증 — 내재가치 안전마진</div>'
                    '<div class="analysis-card-subtitle">잉여현금흐름(FCF)을 WACC로 할인한 내재가치 — 현재 주가와의 괴리(안전마진)를 확인</div>'
                    '<div class="analysis-metric-row">'
                    '<div class="analysis-chip"><div class="chip-label">DCF 내재가치</div>'
                    '<div class="chip-value">$' + str(round(dcf_iv, 2)) + '</div></div>'
                    + p_c + m_c + w_c + '</div>'
                    + _warns(wlist)
                    + '<div class="analysis-verdict ' + v_cls + '">' + v_txt + '</div>'
                    '<details class="analysis-hint-details" open><summary>▶ 해석 보기</summary>'
                    '<div class="analysis-hint">' + hint + '</div></details>'
                    '</div>', unsafe_allow_html=True
                )
            else:
                reason = ''
                if fundamentals:
                    if (fundamentals.get('ebit') or 0) <= 0:
                        reason = ' (영업손실 구간 — DCF 산출 불가)'
                    elif fundamentals.get('ebitda') is None:
                        reason = ' (DA 데이터 미확인)'
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">③ DCF 보조 검증 — 내재가치 안전마진</div>'
                    '<div class="analysis-card-subtitle">잉여현금흐름(FCF)을 WACC로 할인한 내재가치 — 현재 주가와의 괴리(안전마진)를 확인</div>'
                    '<div class="analysis-verdict verdict-wait">⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다' + reason + '</div></div>',
                    unsafe_allow_html=True)
        else:
            render_premium_lock('🔬', 'DCF 보조 검증 — 내재가치 안전마진',
                'WACC 할인율을 적용한 DCF 모델로 내재가치를 산출하고 현재가 대비 안전마진(30% 이상 권장)을 확인합니다.')

        # ④ Reverse DCF ───────────────────────────────────────────────
        if PREMIUM_UNLOCKED:
            rdcf_g  = fundamentals.get('rdcf_implied_g')    if fundamentals else None
            wacc_u  = fundamentals.get('wacc_used')         if fundamentals else None
            fcf_v   = fundamentals.get('fcf')               if fundamentals else None
            price_v = fundamentals.get('current_price')     if fundamentals else None
            rev_g   = fundamentals.get('revenue_growth_yoy') if fundamentals else None
            if rdcf_g is not None:
                if rdcf_g < 0:
                    v_cls, v_txt = 'verdict-buy',   '✅ 성장 없어도 주가 정당 — 보수적 저평가 신호'
                elif rdcf_g <= 5:
                    v_cls, v_txt = 'verdict-watch', '📌 저성장 (' + str(rdcf_g) + '%/yr) 내재 — 적정 밸류에이션'
                elif rdcf_g <= 15:
                    v_cls, v_txt = 'verdict-watch', '📌 중성장 (' + str(rdcf_g) + '%/yr) 내재 — 기대치 점검 필요'
                elif rdcf_g <= 30:
                    v_cls, v_txt = 'verdict-pass',  '⚠️ 고성장 (' + str(rdcf_g) + '%/yr) 내재 — 달성 여부가 핵심'
                else:
                    v_cls, v_txt = 'verdict-pass',  '🚨 초고성장 (' + str(rdcf_g) + '%/yr) 내재 — 투기적 프리미엄'
                price_str = ('현재가 $' + str(round(price_v, 2)) + ' 기준, ' if price_v else '')
                wacc_str  = ('WACC ' + str(round(wacc_u, 1)) + '% 적용 → ' if wacc_u else '')
                rev_cmp   = ''
                if rev_g is not None:
                    diff = round(rdcf_g - rev_g, 1)
                    if diff > 5:
                        rev_cmp = (' 실제 매출성장 ' + str(round(rev_g, 1)) + '%보다 ' + str(diff) + '%p 높은 성장을 내재 — 달성 부담 큼.'
                                   ' 성장 기대 미달 시 멀티플 압축 리스크.')
                    elif diff < -5:
                        rev_cmp = (' 실제 매출성장 ' + str(round(rev_g, 1)) + '%보다 ' + str(abs(diff)) + '%p 낮은 성장만 내재'
                                   ' → 시장이 보수적으로 평가 중. 성장 지속 시 상승 여력 존재.')
                    else:
                        rev_cmp = ' 과거 실제 매출성장(' + str(round(rev_g, 1)) + '%)와 유사한 수준 내재.'
                if rdcf_g < 0:
                    hint = price_str + wacc_str + '현재 EV 기준 FCF/EV가 WACC 초과 → 제로 성장으로도 주가 정당화. 저평가 가능성 높음.'
                elif rdcf_g <= 5:
                    hint = price_str + wacc_str + '내재 성장률 ' + str(rdcf_g) + '%/yr — GDP 성장률 수준.' + rev_cmp
                elif rdcf_g <= 15:
                    hint = price_str + wacc_str + '내재 성장률 ' + str(rdcf_g) + '%/yr — 중성장 구간.' + rev_cmp + ' 과거 추세 대비 달성 가능성 판단 권장.'
                elif rdcf_g <= 30:
                    hint = price_str + wacc_str + '내재 성장률 ' + str(rdcf_g) + '%/yr — 고성장 구간.' + rev_cmp + ' 성장 미달 시 멀티플 압축 리스크.'
                else:
                    hint = price_str + wacc_str + '내재 성장률 ' + str(rdcf_g) + '%/yr — 비현실적 수준.' + rev_cmp + ' 성장 기대 실망 시 급격한 조정 위험.'
                f_c = ('<div class="analysis-chip"><div class="chip-label">FCF (연간)</div>'
                       '<div class="chip-value">$' + str(round(fcf_v / 1e9, 1)) + 'B</div></div>') if fcf_v else ''
                w_c = ('<div class="analysis-chip"><div class="chip-label">WACC</div>'
                       '<div class="chip-value">' + str(round(wacc_u, 1)) + '%</div></div>') if wacc_u else ''
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">④ Reverse DCF — 주가 내재 성장률</div>'
                    '<div class="analysis-card-subtitle">"현재 주가가 정당하려면 매년 몇% 성장해야 하는가?" — 시장의 성장 기대치를 수치로 역산</div>'
                    '<div class="analysis-metric-row">'
                    '<div class="analysis-chip"><div class="chip-label">내재 성장률 (g)</div>'
                    '<div class="chip-value">' + str(rdcf_g) + '%/yr</div></div>'
                    + f_c + w_c + '</div>'
                    '<div class="analysis-verdict ' + v_cls + '">' + v_txt + '</div>'
                    '<details class="analysis-hint-details" open><summary>▶ 해석 보기</summary>'
                    '<div class="analysis-hint">' + hint + '</div></details>'
                    '</div>', unsafe_allow_html=True
                )
            else:
                reason = ''
                if fundamentals:
                    if not fundamentals.get('fcf') or (fundamentals.get('fcf') or 0) <= 0:
                        reason = ' (FCF 음수 — 역DCF 산출 불가)'
                    elif not fundamentals.get('wacc_used'):
                        reason = ' (WACC 미확인)'
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">④ Reverse DCF — 주가 내재 성장률</div>'
                    '<div class="analysis-card-subtitle">"현재 주가가 정당하려면 매년 몇% 성장해야 하는가?" — 시장의 성장 기대치를 수치로 역산</div>'
                    '<div class="analysis-verdict verdict-wait">⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다' + reason + '</div></div>',
                    unsafe_allow_html=True)
        else:
            render_premium_lock('🔄', 'Reverse DCF — 주가 내재 성장률',
                '현재 주가에 시장이 요구하는 성장률을 역산합니다. 낮을수록 기대치 달성 부담이 적습니다.')

        # ⑤ Scenario DCF (Bear / Base / Bull) ────────────────────────
        if PREMIUM_UNLOCKED:
            bear_dcf = fundamentals.get('bear_dcf') if fundamentals else None
            base_dcf = fundamentals.get('base_dcf') if fundamentals else None
            bull_dcf = fundamentals.get('bull_dcf') if fundamentals else None
            price_v  = fundamentals.get('current_price') if fundamentals else None
            wacc_u   = fundamentals.get('wacc_used')     if fundamentals else None
            if any(v is not None for v in [bear_dcf, base_dcf, bull_dcf]):
                def _sc_chip(label, val, pv, sc_cls):
                    if val is None:
                        return ('<div class="scenario-chip ' + sc_cls + '">'
                                '<div class="sc-label">' + label + '</div>'
                                '<div class="sc-value">—</div>'
                                '<div class="sc-marker"></div></div>')
                    ud = round((val - pv) / pv * 100) if pv and pv > 0 else None
                    ud_str = (('+' if ud > 0 else '') + str(ud) + '%') if ud is not None else ''
                    ud_cls = 'sc-up' if (ud or 0) > 0 else 'sc-down'
                    pv_str = ('$' + str(round(pv, 1))) if pv else '?'
                    return ('<div class="scenario-chip ' + sc_cls + '">'
                            '<div class="sc-label">' + label + '</div>'
                            '<div class="sc-value">$' + str(round(val, 2)) + '</div>'
                            '<div class="sc-marker ' + ud_cls + '">' + ud_str + ' vs ' + pv_str + '</div></div>')
                bear_h = _sc_chip('🐻 Bear', bear_dcf, price_v, 'bear')
                base_h = _sc_chip('📊 Base', base_dcf, price_v, 'base')
                bull_h = _sc_chip('🐂 Bull', bull_dcf, price_v, 'bull')
                if base_dcf and price_v and price_v > 0:
                    bm = (base_dcf - price_v) / base_dcf * 100
                    if bm > 30:
                        v_cls5, v_txt5 = 'verdict-buy',   '✅ Base 시나리오 안전마진 ' + str(round(bm)) + '% — 저평가 신호'
                    elif bm > 0:
                        v_cls5, v_txt5 = 'verdict-watch', '📌 Base 시나리오 ' + str(round(bm)) + '% 여유 — 적정~소폭 저평가'
                    else:
                        v_cls5, v_txt5 = 'verdict-pass',  '⚠️ Base 시나리오 현재가 ' + str(round(-bm)) + '% 초과 — 프리미엄 반영'
                else:
                    v_cls5, v_txt5 = 'verdict-wait', '현재가 데이터 확인 중'
                hint5 = ('Bear(WACC+2%, g=1.5%): 경기 침체·성장 둔화 시나리오. '
                         'Base(현재 WACC, g=2.5%): 현황 유지 기본 시나리오. '
                         'Bull(WACC-1%, FCF×1.5, g=4%): 업사이드 달성·금리 하락 시나리오. '
                         '세 값 모두 현재가 상회 시 강한 저평가 신호.')
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">⑤ Scenario DCF — Bull / Base / Bear 내재가치</div>'
                    '<div class="analysis-card-subtitle">성장 시나리오별 DCF 내재가치 — 낙관·기본·비관 범위 확인</div>'
                    '<div class="scenario-row">' + bear_h + base_h + bull_h + '</div>'
                    '<div class="analysis-verdict ' + v_cls5 + '">' + v_txt5 + '</div>'
                    '<details class="analysis-hint-details" open><summary>▶ 해석 보기</summary>'
                    '<div class="analysis-hint">' + hint5 + '</div></details>'
                    '</div>', unsafe_allow_html=True
                )
            else:
                reason5 = ''
                if fundamentals and (fundamentals.get('fcf') or 0) <= 0:
                    reason5 = ' (FCF 음수 — 시나리오 DCF 산출 불가)'
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">⑤ Scenario DCF — Bull / Base / Bear 내재가치</div>'
                    '<div class="analysis-card-subtitle">성장 시나리오별 DCF 내재가치 — 낙관·기본·비관 범위 확인</div>'
                    '<div class="analysis-verdict verdict-wait">⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다' + reason5 + '</div></div>',
                    unsafe_allow_html=True)
        else:
            render_premium_lock('📐', 'Scenario DCF — Bull / Base / Bear',
                '낙관·기본·비관 3가지 시나리오로 DCF 내재가치 범위를 산출합니다.')

        # ⑥ PSR (EV/Revenue) ─────────────────────────────────────────
        if PREMIUM_UNLOCKED:
            psr_v = fundamentals.get('psr')                if fundamentals else None
            rev_g = fundamentals.get('revenue_growth_yoy') if fundamentals else None
            if psr_v is not None:
                p_cls = 'positive' if psr_v < 5 else ('negative' if psr_v > 15 else '')
                if psr_v < 2:
                    v_cls6, v_txt6 = 'verdict-buy',   '✅ 저PSR (< 2x) — 매출 대비 매우 저렴'
                elif psr_v < 5:
                    v_cls6, v_txt6 = 'verdict-buy',   '✅ 저PSR (' + str(round(psr_v, 1)) + 'x) — 합리적 매출 배수'
                elif psr_v < 10:
                    v_cls6, v_txt6 = 'verdict-watch', '📌 중PSR (' + str(round(psr_v, 1)) + 'x) — 성장 프리미엄 수준'
                elif psr_v < 20:
                    v_cls6, v_txt6 = 'verdict-pass',  '⚠️ 고PSR (' + str(round(psr_v, 1)) + 'x) — 강한 성장 지속 필수'
                else:
                    v_cls6, v_txt6 = 'verdict-pass',  '🚨 초고PSR (' + str(round(psr_v, 1)) + 'x) — 투기적 프리미엄'
                rg_c = ''
                if rev_g is not None:
                    rg_cls = 'positive' if rev_g > 20 else ('negative' if rev_g < 0 else '')
                    rg_c = ('<div class="analysis-chip"><div class="chip-label">매출성장(YoY)</div>'
                            '<div class="chip-value ' + rg_cls + '">'
                            + ('+' if rev_g > 0 else '') + str(round(rev_g, 1)) + '%</div></div>')
                growth_req = round(psr_v * 0.3, 0) if psr_v else 0  # PSR×0.3 = 정당화에 필요한 대략적 성장률
                hint6 = ('현재 PSR ' + str(round(psr_v, 1)) + 'x → 매출 $1당 $' + str(round(psr_v, 1)) + ' 지불. '
                         + ('저PSR 구간 — 흑자 전환 시 강한 업사이드 기대.' if psr_v < 5
                            else ('중PSR 구간 — 연간 ' + str(int(growth_req)) + '%+ 성장이 지속되어야 정당화 가능.'
                                  + (' 성장 둔화 시 멀티플 압축 리스크.' if psr_v >= 8 else '')) if psr_v < 15
                            else ('고PSR 구간 — 연간 ' + str(int(growth_req)) + '%+ 고성장이 지속되어야 정당화 가능.'
                                  ' 성장 둔화 시 급격한 멀티플 압축 위험.'))
                         + (' 실제 매출성장 ' + str(round(rev_g, 1)) + '%와 조합 시 '
                            + ('PSR/성장 균형 양호.' if rev_g > 0 and psr_v / max(rev_g, 1) < 0.5
                               else 'PSR 대비 성장 부담 — 성장 둔화 시 리밸류에이션 필요.')
                            if rev_g and rev_g > 0 else ''))
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">⑥ PSR — EV/Revenue 매출 배수</div>'
                    '<div class="analysis-card-subtitle">매출 $1당 시장이 얼마를 지불하는가 — 적자 기업·초기 고성장 기업의 가치 측정에 활용</div>'
                    '<div class="analysis-metric-row">'
                    '<div class="analysis-chip"><div class="chip-label">PSR (EV/Rev)</div>'
                    '<div class="chip-value ' + p_cls + '">' + str(round(psr_v, 1)) + 'x</div></div>'
                    + rg_c + '</div>'
                    '<div class="analysis-verdict ' + v_cls6 + '">' + v_txt6 + '</div>'
                    '<details class="analysis-hint-details" open><summary>▶ 해석 보기</summary>'
                    '<div class="analysis-hint">' + hint6 + '</div></details>'
                    '</div>', unsafe_allow_html=True
                )
            else:
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">⑥ PSR — EV/Revenue 매출 배수</div>'
                    '<div class="analysis-card-subtitle">매출 $1당 시장이 얼마를 지불하는가 — 적자 기업·초기 고성장 기업의 가치 측정에 활용</div>'
                    '<div class="analysis-verdict verdict-wait">⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다</div></div>',
                    unsafe_allow_html=True)
        else:
            render_premium_lock('📈', 'PSR — EV/Revenue 매출 배수',
                '적자 기업·초기 성장주를 위한 매출 기반 밸류에이션. 수익성 없는 구간에서도 상대 비교 가능합니다.')

        # ⑦ Rule of 40 ───────────────────────────────────────────────
        if PREMIUM_UNLOCKED:
            r40_v  = fundamentals.get('rule_of_40')         if fundamentals else None
            rev_g  = fundamentals.get('revenue_growth_yoy') if fundamentals else None
            fcf_mg = fundamentals.get('fcf_margin')         if fundamentals else None
            if r40_v is not None:
                pct   = int(min(max(r40_v, 0), 80) / 80 * 100)
                b_cls = 'pass' if r40_v >= 40 else ('watch' if r40_v >= 20 else 'fail')
                if r40_v >= 40:
                    v_cls7, v_txt7 = 'verdict-buy',   '✅ Rule of 40 충족 (' + str(round(r40_v, 1)) + ') — 성장·수익성 균형 달성'
                elif r40_v >= 20:
                    v_cls7, v_txt7 = 'verdict-watch', '⚠️ Rule of 40 미충족 (' + str(round(r40_v, 1)) + ') — 성장 또는 수익성 보강 필요'
                else:
                    v_cls7, v_txt7 = 'verdict-pass',  '🚨 Rule of 40 크게 미달 (' + str(round(r40_v, 1)) + ') — 손익 구조 점검 필요'
                rs = str(round(rev_g, 1)) + '%' if rev_g is not None else '?'
                fs = str(round(fcf_mg, 1)) + '%' if fcf_mg is not None else '?'
                rev_g_f   = round(rev_g, 1) if rev_g is not None else None
                fcf_mg_f  = round(fcf_mg, 1) if fcf_mg is not None else None
                stage_note = ''
                if r40_v < 40 and rev_g_f is not None and rev_g_f > 20 and fcf_mg_f is not None and fcf_mg_f < 0:
                    stage_note = ' 아직 수익성이 성장 속도를 따라가지 못하는 단계 — 매출 성장 유지 + FCF 마진 개선 추이 추적 필요.'
                elif r40_v < 40 and rev_g_f is not None and rev_g_f > 20:
                    stage_note = ' 성장은 양호하나 수익성 개선 필요 — FCF 마진 전환 시 R40 기준 충족 가능.'
                hint7 = ('매출성장(' + rs + ') + FCF마진(' + fs + ') = Rule of 40 점수 ' + str(round(r40_v, 1)) + '. '
                         + ('40 이상 — 고성장 SaaS·테크 기업 건전성 기준 충족. 성장·수익성 균형 달성.' if r40_v >= 40
                            else '20~40 — 성장·수익성 한쪽 강화로 40 달성 목표.' if r40_v >= 20
                            else '20 미만 — 양쪽 모두 개선 필요. 성장 둔화 + 손실 구간은 특히 주의.')
                         + stage_note)
                rg_c = ('<div class="analysis-chip"><div class="chip-label">매출성장(YoY)</div>'
                        '<div class="chip-value">' + rs + '</div></div>') if rev_g is not None else ''
                fm_c = ('<div class="analysis-chip"><div class="chip-label">FCF 마진</div>'
                        '<div class="chip-value">' + fs + '</div></div>') if fcf_mg is not None else ''
                r40_bar = ('<div class="r40-bar-wrap"><div class="r40-bar-fill ' + b_cls
                           + '" style="width:' + str(pct) + '%"></div></div>')
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">⑦ Rule of 40 — 성장·수익성 균형 지표</div>'
                    '<div class="analysis-card-subtitle">(매출 성장률%) + (FCF 마진%) ≥ 40이면 건강한 고성장 기업 — SaaS·테크 기업 핵심 지표</div>'
                    '<div class="analysis-metric-row">'
                    '<div class="analysis-chip"><div class="chip-label">Rule of 40</div>'
                    '<div class="chip-value">' + str(round(r40_v, 1)) + '</div></div>'
                    + rg_c + fm_c + '</div>'
                    + r40_bar
                    + '<div class="analysis-verdict ' + v_cls7 + '">' + v_txt7 + '</div>'
                    '<details class="analysis-hint-details" open><summary>▶ 해석 보기</summary>'
                    '<div class="analysis-hint">' + hint7 + '</div></details>'
                    '</div>', unsafe_allow_html=True
                )
            else:
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">⑦ Rule of 40 — 성장·수익성 균형 지표</div>'
                    '<div class="analysis-card-subtitle">(매출 성장률%) + (FCF 마진%) ≥ 40이면 건강한 고성장 기업 — SaaS·테크 기업 핵심 지표</div>'
                    '<div class="analysis-verdict verdict-wait">⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다</div></div>',
                    unsafe_allow_html=True)
        else:
            render_premium_lock('📐', 'Rule of 40 — 성장·수익성 균형',
                '매출 성장률과 FCF 마진의 합이 40 이상이면 건강한 고성장 기업으로 판단합니다.')

    # EDGAR 연동 상태: 오류 시 소형 캡션으로만 표시 (노란 박스 제거)
    if PREMIUM_UNLOCKED and fundamentals and fundamentals.get('error'):
        err_msg = str(fundamentals['error'])
        if 'data load failed' in err_msg.lower():
            st.caption('ℹ️ EDGAR 미등록 종목 — 투자 분석 데이터를 불러올 수 없습니다.')
        else:
            st.caption(f'ℹ️ EDGAR: {err_msg}')



# ── 유틸 ─────────────────────────────────────────────────────────
def kst_now_str():
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')


@st.cache_data(ttl=300)
def load_news():
    return get_today_news()


@st.cache_data(ttl=3600)
def get_archive_summary_cached(ticker: str):
    """아카이브 시트에서 가장 최근 summary_kr 조회 (1시간 캐시)."""
    from utils.sheets import get_latest_ticker_summary
    return get_latest_ticker_summary(ticker)


@st.cache_data(ttl=300)
def load_sentiment_data():
    """SENTIMENT 시트 전체 로드 (5분 캐시)."""
    return get_sentiment()


@st.cache_data(ttl=300)
def load_tickers():
    return get_tickers()


@st.cache_data(ttl=86400)   # 24시간 캐시 — EDGAR API 부하 최소화
def fetch_premium_fundamentals(ticker_sym: str,
                                current_price: float = 0,
                                market_cap: float = 0,
                                industry_override: str = '') -> dict:
    """
    EDGAR + Damodaran 연동으로 프리미엄 분석 데이터 산출 (24시간 캐시).
    industry_override: 사용자 지정 Damodaran 업종명 (빈 문자열이면 자동감지).
    """
    api_key = os.environ.get('FINNHUB_API_KEY', '')

    shares_outstanding = None
    if market_cap and current_price and current_price > 0:
        shares_outstanding = market_cap * 1_000_000 / current_price

    try:
        raw = get_edgar_fundamentals(
            ticker=ticker_sym,
            finnhub_api_key=api_key,
            current_price=current_price or None,
            shares_outstanding=shares_outstanding,
            market_cap=market_cap or None,
        )
        if shares_outstanding:
            raw['shares_outstanding'] = shares_outstanding
        enriched = enrich_fundamentals(raw, industry_override=industry_override or None)
        return enriched
    except Exception as e:
        print(f'[Premium] {ticker_sym} 펀더멘탈 계산 오류: {e}')
        return {'error': str(e), 'debug': {'ticker': ticker_sym}}



# ── 경제 캘린더 ──────────────────────────────────────────────
_ECO_KR_EXACT = {
    # ── 고용 ───────────────────────────────────────────────────────
    "Initial Jobless Claims":              "신규 실업수당 청구건수",
    "Continuing Jobless Claims":           "연속 실업수당 청구건수",
    "Nonfarm Payrolls":                    "비농업부문 고용지수",
    "Unemployment Rate":                   "실업률",
    "Participation Rate":                  "경제활동참가율",
    "ADP Employment Change":               "ADP 민간고용변화",
    "Average Hourly Earnings":             "평균 시간당 임금",
    "Average Weekly Hours":                "평균 주당 근로시간",
    "JOLTS Job Openings":                  "JOLTS 구인건수",
    "JOLTS Quits":                         "JOLTS 자발적 이직",
    "Job Openings":                        "구인건수",
    "Challenger Job Cuts":                 "챌린저 감원 발표",
    # ── 물가 ───────────────────────────────────────────────────────
    "CPI":                                 "소비자물가지수",
    "Core CPI":                            "근원 소비자물가지수",
    "PPI":                                 "생산자물가지수",
    "Core PPI":                            "근원 생산자물가지수",
    "PCE Price Index":                     "PCE 물가지수",
    "Core PCE Price Index":                "근원 PCE 물가지수",
    "PCE":                                 "개인소비지출 물가지수",
    "Import Price Index":                  "수입물가지수",
    "Export Price Index":                  "수출물가지수",
    "GDP Price Index":                     "GDP 디플레이터",
    "GDP Deflator":                        "GDP 디플레이터",
    "Trimmed Mean PCE":                    "절사평균 PCE 물가지수",
    # ── GDP · 성장 ─────────────────────────────────────────────────
    "GDP Growth Rate":                     "GDP 성장률",
    "GDP":                                 "GDP",
    "GNP":                                 "국민총생산",
    "Real GDP":                            "실질 GDP",
    # ── 소비 ───────────────────────────────────────────────────────
    "Retail Sales":                        "소매판매",
    "Core Retail Sales":                   "근원 소매판매",
    "Personal Income":                     "개인소득",
    "Personal Spending":                   "개인지출",
    "Consumer Credit":                     "소비자신용",
    "Consumer Confidence":                 "소비자신뢰지수",
    "Michigan Consumer Sentiment":         "미시건 소비자심리지수",
    "Michigan Consumer Expectations":      "미시건 소비자기대지수",
    "Michigan Inflation Expectations":     "미시건 기대인플레이션",
    "CB Consumer Confidence":              "콘퍼런스보드 소비자신뢰지수",
    # ── 제조업 · 생산 ───────────────────────────────────────────────
    "ISM Manufacturing PMI":               "ISM 제조업 PMI",
    "ISM Non-Manufacturing PMI":           "ISM 비제조업 PMI",
    "ISM Services PMI":                    "ISM 서비스업 PMI",
    "S&P Global Manufacturing PMI":        "S&P 글로벌 제조업 PMI",
    "S&P Global Services PMI":             "S&P 글로벌 서비스업 PMI",
    "S&P Global Composite PMI":            "S&P 글로벌 종합 PMI",
    "Markit Manufacturing PMI":            "마킷 제조업 PMI",
    "Markit Services PMI":                 "마킷 서비스업 PMI",
    "Empire State Manufacturing Index":    "엠파이어스테이트 제조업지수",
    "Philadelphia Fed Manufacturing Index":"필라델피아 연준 제조업지수",
    "Chicago PMI":                         "시카고 PMI",
    "Industrial Production":               "산업생산지수",
    "Manufacturing Production":            "제조업 생산지수",
    "Capacity Utilization Rate":           "설비가동률",
    "Factory Orders":                      "공장주문",
    "Durable Goods Orders":                "내구재 주문",
    "Core Durable Goods Orders":           "근원 내구재 주문",
    # ── 주택 ───────────────────────────────────────────────────────
    "NAHB Housing Market Index":           "NAHB 주택시장지수",
    "Housing Starts":                      "주택착공건수",
    "Building Permits":                    "건축허가건수",
    "Existing Home Sales":                 "기존주택판매건수",
    "New Home Sales":                      "신규주택판매건수",
    "Pending Home Sales":                  "주택매매 계약건수",
    "S&P/CS HPI Composite":               "케이스-실러 주택가격지수",
    "Case-Shiller Home Price Index":       "케이스-실러 주택가격지수",
    "FHFA House Price Index":              "FHFA 주택가격지수",
    # ── 무역 · 국제수지 ─────────────────────────────────────────────
    "Trade Balance":                       "무역수지",
    "Current Account":                     "경상수지",
    "Current Account Balance":             "경상수지",
    "Goods Trade Balance":                 "상품 무역수지",
    "Balance of Trade":                    "무역수지",
    # ── 금리 · 통화정책 ─────────────────────────────────────────────
    "Fed Interest Rate Decision":          "연준 금리결정",
    "Federal Funds Rate":                  "연방기금금리",
    "FOMC Meeting Minutes":                "FOMC 의사록",
    "FOMC Minutes":                        "FOMC 의사록",
    "Fed Chair Press Conference":          "연준 의장 기자회견",
    "Fed Press Conference":                "연준 기자회견",
    "BoK Interest Rate Decision":          "한국은행 금리결정",
    "BoJ Interest Rate Decision":          "일본은행 금리결정",
    "ECB Interest Rate Decision":          "유럽중앙은행 금리결정",
    "Bank of England Interest Rate":       "영란은행 금리결정",
    "RBA Interest Rate Decision":          "호주중앙은행 금리결정",
    # ── 재정 ───────────────────────────────────────────────────────
    "Federal Budget Balance":              "연방 재정수지",
    "Government Budget":                   "정부 재정수지",
    "Public Sector Net Borrowing":         "공공부문 순차입",
    # ── 에너지 ─────────────────────────────────────────────────────
    "EIA Crude Oil Inventories":           "EIA 원유재고",
    "EIA Gasoline Inventories":            "EIA 휘발유재고",
    "EIA Distillate Inventories":          "EIA 정제유재고",
    "Crude Oil Inventories":               "원유재고",
    "Baker Hughes Oil Rig Count":          "베이커휴즈 오일 리그수",
    "Baker Hughes Total Rig Count":        "베이커휴즈 총 리그수",
    "Natural Gas Storage":                 "천연가스 재고",
    # ── 국채 입찰 ───────────────────────────────────────────────────
    "3-Month Bill Auction":                "3개월물 국채 입찰",
    "6-Month Bill Auction":                "6개월물 국채 입찰",
    "2-Year Note Auction":                 "2년물 국채 입찰",
    "3-Year Note Auction":                 "3년물 국채 입찰",
    "5-Year Note Auction":                 "5년물 국채 입찰",
    "7-Year Note Auction":                 "7년물 국채 입찰",
    "10-Year Note Auction":                "10년물 국채 입찰",
    "20-Year Bond Auction":                "20년물 국채 입찰",
    "30-Year Bond Auction":                "30년물 국채 입찰",
    # ── 재고 ───────────────────────────────────────────────────────
    "Wholesale Inventories":               "도매재고",
    "Business Inventories":                "기업재고",
    "Retail Inventories":                  "소매재고",
    # ── 한국 주요 지표 ───────────────────────────────────────────────
    "Export Price Index (YoY)":            "수출물가지수(전년비)",
    "Import Price Index (YoY)":            "수입물가지수(전년비)",
    "KR Unemployment Rate":                "한국 실업률",
    "KR Trade Balance":                    "한국 무역수지",
    "KR GDP Growth Rate":                  "한국 GDP 성장률",
    "KR CPI":                              "한국 소비자물가지수",
    # ── 기타 ───────────────────────────────────────────────────────
    "Nonfarm Business Sector Labor Productivity":  "비농업부문 노동생산성",
    "Unit Labor Costs":                    "단위 노동비용",
    "Productivity":                        "노동생산성",
    "Leading Index":                       "경기선행지수",
    "Conference Board Leading Index":      "콘퍼런스보드 경기선행지수",
    "Chicago Fed National Activity Index": "시카고 연준 전국활동지수",
    "Kansas City Fed Manufacturing Index": "캔자스시티 연준 제조업지수",
    "Richmond Fed Manufacturing Index":    "리치먼드 연준 제조업지수",
    "Dallas Fed Manufacturing Index":      "달라스 연준 제조업지수",
    "Dallas Fed Services Index":           "달라스 연준 서비스업지수",
    "Senior Loan Officer Survey":          "고위 대출담당자 설문",
    "Beige Book":                          "베이지북",
    "Redbook":                             "레드북 소매판매",
    "API Weekly Crude Oil Stock":          "API 주간 원유재고",
    "S&P Global PMI":                      "S&P 글로벌 PMI",
    "Flash Manufacturing PMI":             "제조업 PMI (속보치)",
    "Flash Services PMI":                  "서비스업 PMI (속보치)",
    "Flash Composite PMI":                 "종합 PMI (속보치)",
    "Foreign Bond Investment":             "외국인 채권 투자",
    "Foreign Stock Investment":            "외국인 주식 투자",
    "Money Supply M2":                     "통화량(M2)",
    "Money Supply M3":                     "통화량(M3)",
    "Treasury International Capital":     "재무부 국제 자본흐름",
}
_ECO_SUFFIX = [
    (" (MoM)",    " (전월비)"),
    (" (YoY)",    " (전년비)"),
    (" (QoQ)",    " (전분기비)"),
    (" (Prelim)", " (예비치)"),
    (" (Prel)",   " (예비치)"),
    (" (Final)",  " (확정치)"),
    (" (Adv)",    " (속보치)"),
    (" (Flash)",  " (속보치)"),
    (" (2nd Est)","(2차 추정치)"),
    (" (3rd Est)","(3차 추정치)"),
    (" (Revised)","(수정치)"),
    (" (Actual)", " (실제)"),
    (" (SAR)",    " (계절조정)"),
]
def _translate_eco_event(name: str) -> str:
    """경제 이벤트명 영→한 변환 (현업 용어). 미매칭시 원문 반환."""
    if not name:
        return name
    # 1) 완전 일치
    if name in _ECO_KR_EXACT:
        return _ECO_KR_EXACT[name]
    # 2) 접미사 분리 후 기본명 조회
    stripped, suffix_kr = name, ''
    for sfx_en, sfx_kr in _ECO_SUFFIX:
        if stripped.endswith(sfx_en):
            stripped = stripped[: -len(sfx_en)]
            suffix_kr = sfx_kr
            break
    if stripped in _ECO_KR_EXACT:
        return _ECO_KR_EXACT[stripped] + suffix_kr
    # 3) 추가 접미사 제거 후 재시도
    for sfx_en, sfx_kr in _ECO_SUFFIX:
        if stripped.endswith(sfx_en):
            stripped2 = stripped[: -len(sfx_en)]
            suffix_kr = sfx_kr + suffix_kr
            if stripped2 in _ECO_KR_EXACT:
                return _ECO_KR_EXACT[stripped2] + suffix_kr
    return name  # 미매칭 → 원문
# ── 경제 캘린더 툴팁 사전 (kr_name, 설명, ▲영향, ▼영향) ──────────────
_ECO_TOOLTIP = {
    # ── 고용 ──────────────────────────────────────────────────────────
    "Initial Jobless Claims": (
        "신규 실업수당 청구",
        "한 주간 새롭게 실업수당을 신청한 건수. 매주 목요일 발표되는 고빈도 고용 선행지표.",
        "증가 → 고용 악화 신호 · 경기 둔화 우려 · 금리인하 기대 ↑",
        "감소 → 고용 견조 · 금리인하 지연 가능성 · 달러 강세",
    ),
    "Continuing Jobless Claims": (
        "연속 실업수당 청구",
        "이미 실업급여를 받고 있는 누적 인원. 장기 실업 추세를 파악하는 지표.",
        "증가 → 재취업 어려움 · 소비 위축 우려",
        "감소 → 노동시장 흡수력 견조",
    ),
    "Nonfarm Payrolls": (
        "비농업부문 고용",
        "농업을 제외한 전 산업 신규 취업자 수. 월간 최대 이벤트로 발표 즉시 외환·채권·주식 전반에 급격한 변동성을 유발.",
        "예상 상회 → 경기 견조 · 달러 강세 · 금리인상 기대 · 성장주 부담",
        "예상 하회 → 경기 둔화 · 금리인하 기대 · 달러 약세 · 채권 강세",
    ),
    "Unemployment Rate": (
        "실업률",
        "경제활동인구 중 실업자 비율. NFP와 함께 발표되며 고용시장 전반의 건강도를 요약.",
        "상승 → 경기 둔화 신호 · 금리인하 기대 확대",
        "하락 → 완전고용 근접 · 임금 상승 압력 · 금리인하 지연",
    ),
    "ADP Employment Change": (
        "ADP 민간고용",
        "민간 급여 처리 기업 ADP 집계 민간 신규 고용자 수. NFP 이틀 전 발표되는 선행 참고 지표.",
        "예상 상회 → NFP 강세 기대 · 위험선호 심리 ↑",
        "예상 하회 → NFP 부진 경계 · 안전자산 선호",
    ),
    "Average Hourly Earnings": (
        "평균 시간당 임금",
        "민간 비관리직 근로자 평균 시간당 임금. 임금 인플레이션의 핵심 척도로 연준이 중시.",
        "예상 상회 → 임금 인플레 우려 · 금리인상 기대 · 달러 강세",
        "예상 하회 → 임금 압력 완화 · 금리인하 여지 확대",
    ),
    "JOLTS Job Openings": (
        "JOLTS 구인건수",
        "월간 전체 구인 건수. 노동 수요를 직접 측정하며 연준의 고용시장 과열 판단에 활용.",
        "예상 상회 → 노동 수요 강세 · 임금 상승 압력 · 금리인하 지연",
        "예상 하회 → 노동시장 냉각 · 금리인하 기대 ↑",
    ),
    # ── 물가 ──────────────────────────────────────────────────────────
    "CPI": (
        "소비자물가지수",
        "도시 소비자가 구매하는 재화·서비스 가격 변동 측정. 연준 통화정책 결정에 직접 사용되는 핵심 인플레이션 지표.",
        "예상 상회 → 인플레 압력 ↑ · 금리인상 기대 · 달러 강세 · 성장주 약세",
        "예상 하회 → 인플레 완화 · 금리인하 기대 · 성장주·채권 강세",
    ),
    "Core CPI": (
        "근원 소비자물가지수",
        "변동성 큰 식품·에너지를 제외한 CPI. 기저 인플레이션 추세를 보여줘 연준이 헤드라인보다 더 중시.",
        "예상 상회 → 기저 인플레 고착화 우려 · 금리 장기 고수 가능성",
        "예상 하회 → 인플레 둔화 확인 · 금리인하 경로 열림",
    ),
    "PPI": (
        "생산자물가지수",
        "생산자 단계의 재화·서비스 가격. CPI보다 1~3개월 선행하는 인플레이션 선행지표.",
        "예상 상회 → 향후 CPI 상승 압력 예고 · 금리인상 기대",
        "예상 하회 → CPI 둔화 예고 · 금리인하 기대 강화",
    ),
    "Core PPI": (
        "근원 생산자물가지수",
        "식품·에너지 제외 PPI. 기업 마진과 향후 소비자가격 방향성을 가늠하는 지표.",
        "예상 상회 → 기업 비용 증가 → 소비자가격 전가 우려",
        "예상 하회 → 기업 마진 회복 · 가격 안정 신호",
    ),
    "PCE Price Index": (
        "PCE 물가지수",
        "연준 공식 인플레이션 목표 기준 지표(목표치 2%). CPI보다 광범위한 소비 행태를 반영.",
        "예상 상회 → 연준 목표 초과 · 금리인하 지연",
        "예상 하회 → 2% 목표 수렴 · 금리인하 여지 확대",
    ),
    "Core PCE Price Index": (
        "근원 PCE 물가지수",
        "연준이 가장 중시하는 인플레이션 지표. 식품·에너지 제외로 기저 물가 추세를 정확히 반영.",
        "예상 상회 → 금리인하 시점 지연 · 달러 강세",
        "예상 하회 → 금리인하 가시화 · 위험자산 강세",
    ),
    "Import Price Index": (
        "수입물가지수",
        "수입 재화·서비스 가격 변동. 달러 강약과 원자재 가격의 국내 물가 파급을 측정.",
        "예상 상회 → 수입 인플레 확대 · 무역적자 우려",
        "예상 하회 → 수입 디플레 · 소비자가격 안정",
    ),
    # ── GDP ───────────────────────────────────────────────────────────
    "GDP Growth Rate": (
        "GDP 성장률",
        "국내총생산 증가율로 경제 성장의 핵심 지표. 속보(Advance)→예비(Prelim)→확정(Final) 3단계 발표.",
        "예상 상회 → 경기 강세 확인 · 금리인하 지연 가능 · 달러 강세",
        "예상 하회 → 경기 둔화 우려 · 금리인하 기대 · 안전자산 선호",
    ),
    # ── 소비 ──────────────────────────────────────────────────────────
    "Retail Sales": (
        "소매판매",
        "소매업체 매출 총액. GDP의 약 70%를 차지하는 소비 동향을 월간 측정하는 핵심 지표.",
        "예상 상회 → 소비 견조 · 경기 낙관 · 금리인하 지연",
        "예상 하회 → 소비 위축 · 경기 둔화 · 금리인하 기대",
    ),
    "Core Retail Sales": (
        "근원 소매판매",
        "자동차 제외 소매판매. 변동성 큰 자동차 영향을 배제해 소비 기저 추세를 파악.",
        "예상 상회 → 기저 소비력 견조",
        "예상 하회 → 소비심리 약화 신호",
    ),
    "Michigan Consumer Sentiment": (
        "미시건 소비자심리지수",
        "향후 6~12개월 소비자 경기 전망 서베이. 소비 선행지표이자 물가기대 포함으로 연준 주목.",
        "예상 상회 → 소비 강세 기대 · 위험자산 선호",
        "예상 하회 → 소비 위축 우려 · 경기 하강 신호",
    ),
    "Michigan Inflation Expectations": (
        "미시건 기대인플레이션",
        "소비자가 예상하는 1년·5년 인플레이션. 연준의 인플레 기대 고착화 판단에 핵심 자료.",
        "상승 → 인플레 기대 고착 우려 · 연준 매파 전환 압력",
        "하락 → 인플레 기대 안정 · 금리인하 여지 확대",
    ),
    "CB Consumer Confidence": (
        "콘퍼런스보드 소비자신뢰지수",
        "현재 경제 상황과 6개월 전망을 종합한 소비자 신뢰 지수. 미시건 지수와 함께 양대 소비 심리지표.",
        "예상 상회 → 소비 심리 개선 · 위험자산 강세",
        "예상 하회 → 경기 불안 확산 · 방어주 선호",
    ),
    "Personal Income": (
        "개인소득",
        "임금·배당·이자·이전소득 등 가계 총소득. 소비 지속 가능성과 저축 여력을 판단.",
        "예상 상회 → 소비 여력 확대 · 경기 낙관",
        "예상 하회 → 소비 여력 감소 · 경기 둔화 우려",
    ),
    "Personal Spending": (
        "개인지출",
        "가계 실제 소비 지출. PCE와 함께 발표되며 GDP 소비 구성 요소를 실시간 반영.",
        "예상 상회 → 소비 견조 · 경기 강세",
        "예상 하회 → 소비 약화 · 경기 둔화 신호",
    ),
    # ── 제조업·PMI ────────────────────────────────────────────────────
    "ISM Manufacturing PMI": (
        "ISM 제조업 PMI",
        "미국 제조업 구매담당자 설문. 50 이상이면 확장, 미만이면 수축. 경기 방향성을 빠르게 포착.",
        "50 상회·예상 상회 → 제조 경기 확장 · 원자재 강세",
        "50 하회·예상 하회 → 제조 경기 수축 · 경기 둔화 우려",
    ),
    "ISM Services PMI": (
        "ISM 서비스업 PMI",
        "서비스업 구매담당자 설문. 미국 GDP의 약 80%를 점하는 서비스 경기 동향 측정.",
        "50 상회·예상 상회 → 서비스 경기 견조 · 소비 강세 확인",
        "50 하회·예상 하회 → 서비스 경기 수축 · 경기 침체 우려",
    ),
    "S&P Global Manufacturing PMI": (
        "S&P 글로벌 제조업 PMI",
        "ISM PMI와 달리 글로벌 기준으로 산출. 속보치(Flash)는 ISM보다 약 1주 앞서 발표돼 선행 참고값으로 활용.",
        "50 상회·예상 상회 → 제조업 회복 기대",
        "50 하회·예상 하회 → 글로벌 제조 수요 둔화",
    ),
    "S&P Global Services PMI": (
        "S&P 글로벌 서비스업 PMI",
        "글로벌 기준 서비스업 경기 지수. 복합 PMI와 함께 경기 사이클 조기 감지에 활용.",
        "50 상회·예상 상회 → 서비스 회복 · 위험자산 선호",
        "50 하회·예상 하회 → 서비스 둔화 · 안전자산 선호",
    ),
    "Empire State Manufacturing Index": (
        "엠파이어스테이트 제조업지수",
        "뉴욕 연준이 집계하는 뉴욕주 제조업 현황 서베이. 월초 발표되어 해당 월 경기 방향을 가장 먼저 시사.",
        "예상 상회 → 제조 경기 낙관 · 위험선호",
        "예상 하회 → 제조 경기 비관 · 경계심 확대",
    ),
    "Philadelphia Fed Manufacturing Index": (
        "필라델피아 연준 제조업지수",
        "필라델피아 연준 관할 중부대서양 지역 제조업 서베이. 엠파이어 지수와 함께 ISM 선행지표로 활용.",
        "예상 상회 → 지역 제조 견조 · ISM 강세 예고",
        "예상 하회 → 지역 제조 위축 · ISM 부진 경계",
    ),
    "Industrial Production": (
        "산업생산지수",
        "제조업·광업·유틸리티 실제 생산량 지수. 경제 공급 측면의 건강도를 직접 측정.",
        "예상 상회 → 생산 증가 · 경기 확장 확인",
        "예상 하회 → 생산 감소 · 경기 둔화 신호",
    ),
    "Capacity Utilization Rate": (
        "설비가동률",
        "생산 잠재력 대비 실제 가동 비율. 80% 이상은 인플레 압력 신호, 70% 미만은 과잉 공급 우려.",
        "예상 상회·80% 접근 → 생산 과열 · 인플레 압력 ↑",
        "예상 하회 → 생산 여유 · 인플레 완화",
    ),
    "Durable Goods Orders": (
        "내구재 주문",
        "3년 이상 사용 제품(항공기·기계·전자) 주문량. 기업 설비투자 선행지표이나 항공기 영향으로 월간 변동성 큼.",
        "예상 상회 → 기업 투자 의지 강화 · 경기 낙관",
        "예상 하회 → 투자 위축 · 경기 불확실성 확대",
    ),
    "Core Durable Goods Orders": (
        "근원 내구재 주문",
        "변동성 큰 방산·항공 제외 내구재 주문. 기업 설비투자 의향을 가장 순수하게 반영하는 지표.",
        "예상 상회 → 기업 설비투자 확대 · 경기 강세",
        "예상 하회 → 투자 보류 · 경기 경계",
    ),
    # ── 주택 ──────────────────────────────────────────────────────────
    "NAHB Housing Market Index": (
        "NAHB 주택시장지수",
        "전미주택건설업협회(NAHB) 주택건설업자 경기 서베이. 50 기준으로 주택 경기 낙관·비관을 판단.",
        "50 상회·예상 상회 → 주택 경기 낙관 · 관련주 강세",
        "50 하회·예상 하회 → 주택 수요 위축 우려",
    ),
    "Housing Starts": (
        "주택착공건수",
        "신규 주택 공사 시작 건수. 건설·자재 수요를 선행하며 주택 공급 동향을 직접 측정.",
        "예상 상회 → 주택 공급 확대 · 건설 경기 강세",
        "예상 하회 → 공급 위축 · 주택가격 상승 압력",
    ),
    "Building Permits": (
        "건축허가건수",
        "향후 1~2개월 착공 예정 물량. 주택착공의 선행지표로 주택 경기 방향성 예측에 활용.",
        "예상 상회 → 향후 착공 증가 예고 · 주택 공급 확대",
        "예상 하회 → 착공 감소 예고 · 공급 부족 우려",
    ),
    "Existing Home Sales": (
        "기존주택판매건수",
        "중고 주택 거래량. 전체 주택거래의 약 90% 비중으로 주택시장 온도계 역할.",
        "예상 상회 → 주택 수요 견조 · 부동산 경기 강세",
        "예상 하회 → 주택 거래 위축 · 고금리 부담 확인",
    ),
    "New Home Sales": (
        "신규주택판매건수",
        "신규 분양 주택 계약 건수. 건설사 수주와 연동되며 기존주택 대비 선행성이 강함.",
        "예상 상회 → 신규 수요 견조 · 건설주 호재",
        "예상 하회 → 신규 분양 부진 · 고금리 충격 가시화",
    ),
    "Pending Home Sales": (
        "주택매매 계약건수",
        "매매 계약 체결 후 아직 완료되지 않은 건수. 1~2개월 후 기존주택판매의 선행지표.",
        "예상 상회 → 향후 거래 증가 예고",
        "예상 하회 → 향후 거래 감소 경고",
    ),
    # ── 무역 ──────────────────────────────────────────────────────────
    "Trade Balance": (
        "무역수지",
        "수출액에서 수입액을 뺀 차액. 적자가 커질수록 달러 수요 감소 · 경상수지 악화로 이어질 수 있음.",
        "적자 축소 → 순수출 개선 · 달러 지지",
        "적자 확대 → 경상수지 악화 · 달러 약세 요인",
    ),
    # ── 금리·통화정책 ─────────────────────────────────────────────────
    "Fed Interest Rate Decision": (
        "연준 금리결정",
        "연방공개시장위원회(FOMC) 정책금리 결정. 전 세계 금융시장에 가장 큰 영향을 미치는 단일 이벤트.",
        "인상 or 매파 → 달러 강세 · 채권 약세 · 성장주 부담",
        "인하 or 비둘기파 → 위험자산 강세 · 채권 강세 · 달러 약세",
    ),
    "FOMC Meeting Minutes": (
        "FOMC 의사록",
        "금리 결정 회의의 상세 논의 공개. 위원별 매파·비둘기파 발언 비율로 향후 금리 방향성 파악.",
        "매파 우세 → 금리인하 지연 기대 · 달러 강세",
        "비둘기파 우세 → 금리인하 기대 확대 · 위험자산 강세",
    ),
    "BoK Interest Rate Decision": (
        "한국은행 금리결정",
        "한국은행 금융통화위원회 기준금리 결정. 원화 가치와 국내 채권·부동산 시장에 직접 영향.",
        "인상 or 동결(매파) → 원화 강세 · 채권 약세",
        "인하 or 비둘기파 → 원화 약세 · 채권 강세 · 주식 지지",
    ),
    # ── 에너지 ────────────────────────────────────────────────────────
    "EIA Crude Oil Inventories": (
        "EIA 원유재고",
        "미국 에너지정보청(EIA) 주간 원유 재고량. 원유 공급·수요 균형을 판단하는 핵심 단기 지표.",
        "예상보다 감소 → 공급 타이트 · 유가 상승 압력 · 에너지주 강세",
        "예상보다 증가 → 공급 과잉 · 유가 하락 압력 · 에너지주 약세",
    ),
    "Natural Gas Storage": (
        "천연가스 재고",
        "EIA 주간 천연가스 저장량. 계절 수요와 비교해 공급 여유분을 판단하며 가스 가격에 직접 영향.",
        "예상보다 감소 → 공급 부족 · 가스 가격 ↑",
        "예상보다 증가 → 공급 여유 · 가스 가격 ↓",
    ),
    # ── 재고·공급망 ───────────────────────────────────────────────────
    "Wholesale Inventories": (
        "도매재고",
        "도매업체 보유 재고. 증가는 수요 부진 또는 과잉 공급 신호, 감소는 수요 회복을 시사.",
        "예상보다 증가 → 수요 둔화 우려 · 향후 생산 감소 가능",
        "예상보다 감소 → 수요 견조 · 재고 보충 기대",
    ),
    "Business Inventories": (
        "기업재고",
        "제조·도매·소매 전 단계 재고 종합. GDP 재고투자 항목에 직접 반영.",
        "예상보다 증가 → 재고 과잉 · 향후 생산 축소 가능",
        "예상보다 감소 → 재고 소진 · 생산 회복 기대",
    ),
    # ── 재정 ──────────────────────────────────────────────────────────
    "Federal Budget Balance": (
        "연방 재정수지",
        "미국 연방정부 세수와 지출의 차이. 적자 확대는 국채 발행 증가로 장기 금리 상승 요인.",
        "흑자 또는 적자 축소 → 국채 공급 감소 · 금리 안정",
        "적자 확대 → 국채 공급 증가 · 장기금리 상승 압력",
    ),
    # ── 기타 체크포인트 ───────────────────────────────────────────────
    "Beige Book": (
        "베이지북",
        "연준 12개 지역 경기 현황 정성적 보고서. FOMC 2주 전 발표되며 정책 결정 배경 이해에 필수.",
        "전반적 확장 기술 → 금리 고수 또는 인상 가능성",
        "전반적 둔화 기술 → 금리인하 논의 확대 기대",
    ),
    "Consumer Credit": (
        "소비자신용",
        "자동차·학자금 등 소비자 대출 잔액 변화. 신용카드 포함 시 소비 여력과 부채 부담 동향 확인.",
        "증가 → 소비 의지 강함 · 향후 지출 증가 기대",
        "감소 → 부채 상환 또는 소비 여력 축소 신호",
    ),
    "Chicago PMI": (
        "시카고 PMI",
        "시카고 지역 제조·서비스 복합 경기 서베이. ISM 전날 발표되어 당일 ISM 방향성 예측에 활용.",
        "50 상회·예상 상회 → ISM 강세 선행 신호",
        "50 하회·예상 하회 → ISM 약세 경계",
    ),
    "Richmond Fed Manufacturing Index": (
        "리치먼드 연준 제조업지수",
        "리치먼드 연준 관할 중부대서양 남부 제조업 서베이. 지역 제조 경기 선행지표.",
        "예상 상회 → 지역 제조 개선",
        "예상 하회 → 지역 제조 위축",
    ),
    "Dallas Fed Manufacturing Index": (
        "달라스 연준 제조업지수",
        "텍사스 등 남서부 제조업 서베이. 에너지 산업 비중이 높아 유가와 함께 해석.",
        "예상 상회 → 남서부 제조·에너지 경기 개선",
        "예상 하회 → 에너지 경기 둔화 반영 가능",
    ),
}
def _get_tooltip(name: str):
    """이벤트명으로 툴팁 정보 조회. 반환: (kr_name, desc, use_up, use_dn) or None."""
    if not name:
        return None
    if name in _ECO_TOOLTIP:
        return _ECO_TOOLTIP[name]
    # 접미사 제거 후 재조회
    for sfx in [" (MoM)"," (YoY)"," (QoQ)"," (Prelim)"," (Prel)",
                " (Final)"," (Adv)"," (Flash)"," (2nd Est)"," (3rd Est)",
                " (Revised)"," (SAR)"," (Actual)"]:
        if name.endswith(sfx):
            base = name[:-len(sfx)]
            if base in _ECO_TOOLTIP:
                return _ECO_TOOLTIP[base]
    return None
@st.cache_data(ttl=1800)
def fetch_economic_calendar():
    """Finnhub 경제캘린더 API — 이번 주 (월~일) 데이터 fetch."""
    api_key = os.environ.get('FINNHUB_API_KEY', '')
    if not api_key:
        return None, 'no_key'
    today  = datetime.now(KST).date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    url = (
        'https://finnhub.io/api/v1/calendar/economic'
        f'?from={monday}&to={sunday}&token={api_key}'
    )
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 403:
            return None, '403'
        r.raise_for_status()
        return r.json().get('economicCalendar', []), 'ok'
    except Exception as e:
        return None, str(e)
def render_economic_calendar():
    """경제 캘린더 사이드바 섹션 — Finnhub API 기반 테이블 (한글명 + 클릭 팝업)."""
    st.markdown(
        '<div style="font-size:0.7rem;color:#a6adc8;font-weight:600;'
        'margin:10px 0 4px 0;">📅 경제 캘린더</div>',
        unsafe_allow_html=True,
    )
    IMP_OPTIONS     = ['🔴 고영향', '🟠 고+중', '⚪ 전체']
    COUNTRY_OPTIONS = ['🇺🇸 미국', '🇺🇸+🇰🇷 미국·한국', '🌐 전체']
    IMP_MAP  = {'🔴 고영향': ['high'], '🟠 고+중': ['high', 'medium'], '⚪ 전체': ['high', 'medium', 'low']}
    CTRY_MAP = {'🇺🇸 미국': ['US'], '🇺🇸+🇰🇷 미국·한국': ['US', 'KR'], '🌐 전체': None}
    FLAG_MAP = {'US':'🇺🇸','KR':'🇰🇷','EU':'🇪🇺','GB':'🇬🇧','JP':'🇯🇵','CN':'🇨🇳','DE':'🇩🇪','CA':'🇨🇦','AU':'🇦🇺'}
    DOT_CLR  = {'high':'#f38ba8', 'medium':'#f9e2af', 'low':'#585b70'}
    with st.expander('📋 이번 주 주요 경제 일정', expanded=False):
        col_l, col_r = st.columns(2)
        with col_l:
            imp = st.selectbox('중요도', IMP_OPTIONS, index=0,
                               key='cal_imp', label_visibility='collapsed')
        with col_r:
            ctry = st.selectbox('국가', COUNTRY_OPTIONS, index=0,
                                key='cal_ctry', label_visibility='collapsed')
        imp_set  = IMP_MAP.get(imp, ['high'])
        ctry_set = CTRY_MAP.get(ctry)
        cal_data, status = fetch_economic_calendar()
        if status == 'no_key':
            st.caption('⚠️ FINNHUB_API_KEY 시크릿이 설정되지 않았습니다.')
            return
        if status == '403':
            st.caption('⚠️ Finnhub 무료 플랜에서 경제캘린더 API가 제한됩니다. (Paid plan 필요)')
            return
        if status != 'ok' or not cal_data:
            st.caption(f'⚠️ 데이터 로드 실패: {status}')
            return
        # ── 필터링 ─────────────────────────────────────────────────
        filtered = []
        for ev in cal_data:
            ev_imp = (ev.get('impact') or '').lower()
            if ev_imp not in imp_set:
                continue
            if ctry_set and ev.get('country', '') not in ctry_set:
                continue
            filtered.append(ev)
        # ── 날짜 내림차순 정렬 ──────────────────────────────────────
        filtered.sort(key=lambda x: x.get('time', ''), reverse=True)
        if not filtered:
            st.caption('해당하는 일정이 없습니다.')
            return
        # ── HTML 테이블 빌드 ────────────────────────────────────────
        today_str = datetime.now(KST).strftime('%Y-%m-%d')
        rows = ''
        last_date = None
        for ev in filtered:
            t      = ev.get('time', '')
            dpart  = t[:10] if len(t) >= 10 else ''
            tpart  = t[11:16] if len(t) >= 16 else ''
            is_today = (dpart == today_str)
            show_date = (dpart != last_date)
            last_date = dpart
            try:
                dt_obj   = datetime.strptime(dpart, '%Y-%m-%d')
                mmdd     = dt_obj.strftime('%m/%d')
                if is_today:
                    date_cell = f'<span style="color:#89b4fa;font-weight:600">{mmdd} 오늘</span>'
                else:
                    date_cell = f'<span style="color:#a6adc8">{mmdd}</span>'
            except Exception:
                date_cell = f'<span style="color:#a6adc8">{dpart}</span>'
            flag    = FLAG_MAP.get(ev.get('country',''), '🌐')
            ev_imp  = (ev.get('impact') or '').lower()
            dot_c   = DOT_CLR.get(ev_imp, '#585b70')
            _raw    = ev.get('event', '')
            # ── 한글 번역 ──────────────────────────────────────────
            evname  = _translate_eco_event(_raw)
            actual  = str(ev.get('actual') or '').strip() or '—'
            est     = str(ev.get('estimate') or '').strip() or '—'
            prev    = str(ev.get('prev') or '').strip() or '—'
            _tip    = _get_tooltip(_raw)
            # 발표값 vs 예측값 색상
            act_color = '#cdd6f4'
            if actual != '—' and est != '—':
                try:
                    def _num(s):
                        return float(s.replace('%','').replace('K','e3').replace('M','e6').replace('B','e9').replace(',',''))
                    a, e = _num(actual), _num(est)
                    act_color = '#a6e3a1' if a >= e else '#f38ba8'
                except Exception:
                    pass
            date_td = f'<td style="padding:5px 4px;white-space:nowrap;font-size:0.7rem">{date_cell if show_date else ""}</td>'
            # ── 이벤트 셀: 툴팁 있으면 클릭 가능 span, 없으면 일반 텍스트 ──
            if _tip:
                def _esc(s): return s.replace('"', '&quot;').replace("'", '&#39;')
                evt_cell = (
                    f'<span class="evn"'
                    f' data-kr="{_esc(_tip[0])}"'
                    f' data-en="{_esc(_raw)}"'
                    f' data-desc="{_esc(_tip[1])}"'
                    f' data-up="{_esc(_tip[2])}"'
                    f' data-dn="{_esc(_tip[3])}"'
                    f'>{evname}</span>'
                )
            else:
                evt_cell = f'<span style="color:#cdd6f4">{evname}</span>'
            rows += (
                f'<tr style="border-bottom:1px solid #313244">'
                f'{date_td}'
                f'<td style="padding:5px 4px;font-size:0.68rem;color:#7f849c;white-space:nowrap">{tpart} ET</td>'
                f'<td style="padding:5px 4px;font-size:0.82rem;text-align:center">{flag}</td>'
                f'<td style="padding:5px 4px;text-align:center">'
                f'<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:{dot_c}"></span>'
                f'</td>'
                f'<td style="padding:5px 6px;font-size:0.7rem">{evt_cell}</td>'
                f'<td style="padding:5px 4px;font-size:0.7rem;color:{act_color};text-align:right;white-space:nowrap">{actual}</td>'
                f'<td style="padding:5px 4px;font-size:0.7rem;color:#7f849c;text-align:right;white-space:nowrap">{est}</td>'
                f'<td style="padding:5px 4px;font-size:0.7rem;color:#585b70;text-align:right;white-space:nowrap">{prev}</td>'
                f'</tr>'
            )
        html_out = (
            '<style>'
            'body{margin:0;padding:0;background:#1e1e2e;color:#cdd6f4;font-family:sans-serif;overflow-x:auto}'
            '.eco-tbl{width:100%;border-collapse:collapse}'
            '.eco-tbl thead th{font-size:0.65rem;color:#6c7086;padding:5px 4px;'
            'border-bottom:1px solid #45475a;font-weight:500;white-space:nowrap}'
            '.eco-tbl thead th.r{text-align:right}'
            '.evn{font-size:0.7rem;color:#89b4fa;border-bottom:1px dashed #45475a;'
            'cursor:pointer;display:inline;transition:opacity 0.15s}'
            '.evn:hover{opacity:0.75}'
            '#eco-modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;'
            'background:rgba(0,0,0,0.55);z-index:9999;align-items:center;justify-content:center}'
            '#eco-modal.open{display:flex}'
            '.modal-card{background:#24273a;border:1px solid #45475a;border-radius:10px;'
            'padding:16px 18px;width:90%;max-width:300px;position:relative;box-sizing:border-box}'
            '#eco-m-close{position:absolute;top:8px;right:10px;background:none;border:none;'
            'color:#a6adc8;font-size:18px;cursor:pointer;line-height:1;padding:0}'
            '#eco-m-close:hover{color:#cdd6f4}'
            '#eco-m-kr{font-size:0.65rem;color:#7f849c;margin-bottom:2px}'
            '#eco-m-en{font-size:0.78rem;font-weight:600;color:#cdd6f4;margin-bottom:8px;'
            'padding-bottom:8px;border-bottom:1px solid #313244}'
            '#eco-m-desc{font-size:0.68rem;color:#a6adc8;line-height:1.55;margin-bottom:10px}'
            '.m-row{font-size:0.67rem;line-height:1.6;margin-top:3px}'
            '.m-up{color:#a6e3a1}'
            '.m-dn{color:#f38ba8}'
            '</style>'
            '<table class="eco-tbl"><thead><tr>'
            '<th>날짜</th><th>시간</th><th></th><th></th>'
            '<th>이벤트</th>'
            '<th class="r">발표</th><th class="r">예측</th><th class="r">이전</th>'
            '</tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            '<div id="eco-modal">'
            '<div class="modal-card">'
            '<button id="eco-m-close">✕</button>'
            '<div id="eco-m-kr"></div>'
            '<div id="eco-m-en"></div>'
            '<div id="eco-m-desc"></div>'
            '<div class="m-row" id="eco-m-up"></div>'
            '<div class="m-row" id="eco-m-dn"></div>'
            '</div></div>'
            '<script>'
            'var modal=document.getElementById("eco-modal");'
            'var mKr=document.getElementById("eco-m-kr");'
            'var mEn=document.getElementById("eco-m-en");'
            'var mDesc=document.getElementById("eco-m-desc");'
            'var mUp=document.getElementById("eco-m-up");'
            'var mDn=document.getElementById("eco-m-dn");'
            'document.querySelectorAll(".evn").forEach(function(el){'
            '  el.addEventListener("click",function(){'
            '    mKr.textContent=el.dataset.kr;'
            '    mEn.textContent=el.dataset.en;'
            '    mDesc.textContent=el.dataset.desc;'
            '    mUp.innerHTML="<span class=\'m-up\'>▲</span> "+el.dataset.up;'
            '    mDn.innerHTML="<span class=\'m-dn\'>▼</span> "+el.dataset.dn;'
            '    modal.classList.add("open");'
            '  });'
            '});'
            'document.getElementById("eco-m-close").addEventListener("click",function(){'
            '  modal.classList.remove("open");});'
            'modal.addEventListener("click",function(e){'
            '  if(e.target===modal) modal.classList.remove("open");});'
            'document.addEventListener("keydown",function(e){'
            '  if(e.key==="Escape") modal.classList.remove("open");});'
            '</script>'
        )
        row_h  = max(220, min(len(filtered) * 34 + 80, 500))
        components.html(html_out, height=row_h, scrolling=True)
        st.markdown(
            '<div style="font-size:0.6rem;color:#6c7086;text-align:right;padding:2px 4px 0 0;">'
            '출처: <a href="https://finnhub.io/" target="_blank"'
            ' style="color:#89b4fa;text-decoration:none;">Finnhub</a></div>',
            unsafe_allow_html=True,
        )


# ── 하이퍼스케일러 분석 ──────────────────────────────────────────────
HYPSCALER_TICKERS = ['MSFT', 'GOOGL', 'AMZN', 'META']

# 2026년 공식 Capex 가이던스 (단위: 십억달러, 캘린더 연간 기준)
# 출처: 각 사 Q1 2026 실적발표 (2026년 4~5월 공시)
HYPSCALER_GUIDANCE = {
    'MSFT':  190,   # Q3 FY2026 실적발표 2026.04.30
    'GOOGL': 185,   # Q1 2026 실적발표 2026.04.29 ($180~190B 중간값)
    'AMZN':  200,   # Q1 2026 실적발표 2026.05.01 (CEO Andy Jassy 공식 발표)
    'META':  135,   # Q1 2026 실적발표 2026.04.29 ($125~145B 중간값)
}
HYPSCALER_GUIDANCE_TOTAL = sum(HYPSCALER_GUIDANCE.values())   # 710
HYPSCALER_GUIDANCE_NOTE  = '2026년 Q1 실적발표 기준 (2026년 4~5월 공시) — 캘린더 연간 기준'

@st.cache_data(ttl=3600)
def fetch_hyperscaler_data():
    """MSFT·GOOGL·AMZN·META 하이브리드 재무 취득
    - 2020~(분기TTM시작 전): 연간 income_stmt (역사 기반)
    - 분기TTM시작~현재: quarterly TTM rolling(4) (분기별 디테일, 계절성 제거)
    → 두 구간 연결해 하나의 연속 시계열 반환
    """
    import yfinance as yf
    import pandas as pd

    NI_ROWS    = ['Net Income', 'Net Income Common Stockholders',
                  'Net Income From Continuing Operations']
    OCF_ROWS   = ['Operating Cash Flow',
                  'Cash Flow From Continuing Operating Activities',
                  'Total Cash From Operating Activities',
                  'Net Cash Provided By Operating Activities']
    CAPEX_ROWS = ['Capital Expenditure',
                  'Purchase Of Property Plant And Equipment',
                  'Capital Expenditure Reported',
                  'Purchases of property and equipment',
                  'Capital Expenditures']

    a_ni, a_ocf, a_cap = {}, {}, {}   # 연간
    q_ni, q_ocf, q_cap = {}, {}, {}   # 분기

    for ticker in HYPSCALER_TICKERS:
        try:
            t = yf.Ticker(ticker)
            # ── 연간 ──
            inc_a = t.income_stmt
            cf_a  = t.cash_flow
            for r in NI_ROWS:
                if r in inc_a.index:
                    a_ni[ticker] = inc_a.loc[r]; break
            for r in OCF_ROWS:
                if r in cf_a.index:
                    a_ocf[ticker] = cf_a.loc[r]; break
            for r in CAPEX_ROWS:
                if r in cf_a.index:
                    a_cap[ticker] = cf_a.loc[r]; break
            # ── 분기 ──
            inc_q = t.quarterly_income_stmt
            cf_q  = t.quarterly_cashflow
            for r in NI_ROWS:
                if r in inc_q.index:
                    q_ni[ticker] = inc_q.loc[r]; break
            for r in OCF_ROWS:
                if r in cf_q.index:
                    q_ocf[ticker] = cf_q.loc[r]; break
            for r in CAPEX_ROWS:
                if r in cf_q.index:
                    q_cap[ticker] = cf_q.loc[r]; break
        except Exception as e:
            print(f'[hyperscaler] {ticker}: {e}')

    if len(a_ni) < 2 and len(q_ni) < 2:
        return None, 'fetch_error'

    def _sum_series(d):
        """여러 종목 Series를 합산해 날짜 인덱스 Series 반환"""
        if not d:
            return pd.Series(dtype=float)
        df = pd.DataFrame(d).fillna(0)
        s  = df.sum(axis=1)
        s.index = pd.to_datetime(s.index)
        return s.sort_index()

    def _ttm(s):
        """분기 Series → TTM rolling(4) 합산, 십억달러"""
        return s.rolling(4).sum().dropna() / 1e9

    def _annual(s):
        """연간 Series → 십억달러"""
        return s / 1e9

    # ── 연간 합산 ─────────────────────────────────────────────────
    ni_a  = _annual(_sum_series(a_ni))
    ocf_a = _annual(_sum_series(a_ocf))
    cap_a = _annual(_sum_series(a_cap))
    fcf_a = ocf_a + cap_a

    # ── 분기 TTM 합산 ─────────────────────────────────────────────
    ni_q  = _ttm(_sum_series(q_ni))
    ocf_q = _ttm(_sum_series(q_ocf))
    cap_q = _ttm(_sum_series(q_cap))
    fcf_q = ocf_q + cap_q

    # ── 병합: 분기 TTM 시작 이전은 연간, 이후는 분기 TTM ─────────
    def _merge(annual_s, qrtly_s):
        if qrtly_s.empty:
            return annual_s
        if annual_s.empty:
            return qrtly_s
        # 분기 TTM 첫 날짜 이전의 연간 데이터만 사용
        cutoff    = qrtly_s.index[0]
        ann_part  = annual_s[annual_s.index < cutoff]
        combined  = pd.concat([ann_part, qrtly_s]).sort_index()
        combined  = combined[~combined.index.duplicated(keep='last')]
        return combined

    ni_final  = _merge(ni_a,  ni_q)
    ocf_final = _merge(ocf_a, ocf_q)
    fcf_final = _merge(fcf_a, fcf_q)

    result = pd.DataFrame({
        'net_income': ni_final,
        'ocf':        ocf_final,
        'fcf':        fcf_final,
    }).sort_index().dropna(subset=['net_income'])

    return result, 'ok'


def render_hyperscaler_tab():
    """하이퍼스케일러 분석 탭 — 순이익 vs FCF 추이 + 2026 Capex 가이던스 반영 추정"""
    import plotly.graph_objects as _go
    import pandas as _pd

    st.markdown(
        '<div style="font-size:0.82rem;color:#a6adc8;margin:4px 0 14px 0;">'
        'MSFT · GOOGL · AMZN · META 합산 — 순이익 vs 잉여현금흐름(FCF) 추이'
        '</div>',
        unsafe_allow_html=True,
    )

    df, status = fetch_hyperscaler_data()
    if status != 'ok' or df is None or df.empty:
        st.warning(f'데이터 로드 실패 ({status}). 잠시 후 새로고침해주세요.')
        return

    latest      = df.iloc[-1]
    latest_date = df.index[-1]
    ni_latest   = float(latest['net_income'])
    fcf_latest  = float(latest['fcf'])

    # ── 요약 수치 카드 ────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric('합산 순이익 (최근 연간)', f'${ni_latest:.0f}B')
    with c2:
        delta_fcf = fcf_latest - ni_latest
        st.metric('합산 FCF (최근 연간)', f'${fcf_latest:.0f}B',
                  delta=f'{delta_fcf:.0f}B', delta_color='inverse')
    with c3:
        gap_pct = (ni_latest - fcf_latest) / ni_latest * 100 if ni_latest else 0
        st.metric('순이익-FCF 갭', f'{gap_pct:.0f}%')
    with c4:
        st.metric('2026 합산 Capex 가이던스', f'${HYPSCALER_GUIDANCE_TOTAL}B',
                  help='각 사 Q1 2026 실적발표 기준')

    st.markdown('<div style="margin:4px 0 10px 0;border-top:1px solid #313244;"></div>',
                unsafe_allow_html=True)

    # ── 1년 추정 계산 ─────────────────────────────────────────────
    fwd_date = latest_date + _pd.DateOffset(years=1)

    if len(df) >= 2:
        ocf_g = float(df['ocf'].iloc[-1] / df['ocf'].iloc[-2] - 1) if df['ocf'].iloc[-2] != 0 else 0.15
        ni_g  = float(df['net_income'].iloc[-1] / df['net_income'].iloc[-2] - 1) if df['net_income'].iloc[-2] != 0 else 0.20
        ocf_g = max(min(ocf_g, 0.45), 0.05)
        ni_g  = max(min(ni_g,  0.50), 0.05)
    else:
        ocf_g, ni_g = 0.15, 0.20

    fwd_ocf = float(latest['ocf']) * (1 + ocf_g)
    fwd_fcf = fwd_ocf - HYPSCALER_GUIDANCE_TOTAL
    fwd_ni  = ni_latest * (1 + ni_g)

    # ── Plotly 차트 ───────────────────────────────────────────────
    fig = _go.Figure()

    # 실제값 — 순이익 (실선, 파란색)
    fig.add_trace(_go.Scatter(
        x=df.index, y=df['net_income'],
        name='순이익 (실제)',
        line=dict(color='#4a9eff', width=2.5),
        mode='lines+markers',
        marker=dict(size=5, color='#4a9eff'),
        hovertemplate='%{x|%Y} 순이익: $%{y:.0f}B<extra></extra>',
    ))

    # 실제값 — FCF (점선, 회색)
    fig.add_trace(_go.Scatter(
        x=df.index, y=df['fcf'],
        name='FCF (실제)',
        line=dict(color='#a6adc8', width=2, dash='dot'),
        mode='lines+markers',
        marker=dict(size=5, color='#a6adc8'),
        hovertemplate='%{x|%Y} FCF: $%{y:.0f}B<extra></extra>',
    ))

    # 추정 — 순이익 (점선, 파란색 옅게)
    fig.add_trace(_go.Scatter(
        x=[latest_date, fwd_date], y=[ni_latest, fwd_ni],
        name=f'순이익 추정 (+{ni_g*100:.0f}% 성장 가정)',
        line=dict(color='#4a9eff', width=1.5, dash='dash'),
        mode='lines+markers',
        marker=dict(size=6, color='#4a9eff', symbol='circle-open'),
        hovertemplate='추정 순이익: $%{y:.0f}B<extra></extra>',
    ))

    # 추정 — FCF (점선, 위험 시 빨간색)
    fcf_clr = '#f38ba8' if fwd_fcf < 0 else '#a6adc8'
    fig.add_trace(_go.Scatter(
        x=[latest_date, fwd_date], y=[fcf_latest, fwd_fcf],
        name=f'FCF 추정 (가이던스 Capex ${HYPSCALER_GUIDANCE_TOTAL}B 반영)',
        line=dict(color=fcf_clr, width=1.5, dash='dash'),
        mode='lines+markers',
        marker=dict(size=6, color=fcf_clr, symbol='circle-open'),
        hovertemplate='추정 FCF: $%{y:.0f}B<extra></extra>',
    ))

    # 현재 수직선
    fig.add_vline(
        x=latest_date, line_dash='dot',
        line_color='#585b70', line_width=1,
    )
    fig.add_annotation(
        x=latest_date, y=0.97, yref='paper',
        text='현재', showarrow=False,
        font=dict(size=9, color='#7f849c'), xshift=20,
    )

    # 추정 구간 음영
    fig.add_vrect(
        x0=latest_date, x1=fwd_date,
        fillcolor='rgba(88,91,112,0.06)', line_width=0,
        annotation_text='추정 구간', annotation_position='top left',
        annotation_font_size=9, annotation_font_color='#585b70',
    )

    # FCF 제로선 강조
    fig.add_hline(y=0, line_dash='solid', line_color='#45475a', line_width=1)

    fig.update_layout(
        height=350,
        margin=dict(l=55, r=20, t=40, b=40),
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#cdd6f4', size=10),
        xaxis=dict(
            gridcolor='#313244', showgrid=True, title='',
            tickformat='%y',   # 20, 21, 22 … 연도 2자리
            dtick='M12',       # 1년 단위 눈금
        ),
        yaxis=dict(
            gridcolor='#313244', showgrid=True,
            title='합산 (십억달러, B)',
            zeroline=False,
        ),
        legend=dict(
            orientation='h', yanchor='bottom', y=1.01,
            xanchor='left', x=0,
            font=dict(size=9), bgcolor='rgba(0,0,0,0)',
        ),
        hovermode='x unified',
    )
    st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})

    # ── 데이터 출처 및 한계 설명 ──────────────────────────────────
    st.markdown(
        '<div style="background:#1e1e2e;border:1px solid #313244;border-radius:8px;'
        'padding:12px 16px;margin:4px 0 8px 0;font-size:0.72rem;color:#7f849c;line-height:1.8;">'
        '<span style="color:#a6adc8;font-weight:600;">📌 데이터 출처 및 구현 한계</span><br>'
        '• <span style="color:#cdd6f4">실선 (실제값)</span> : yfinance — SEC 공시 기반 분기 재무데이터. '
        '순이익·FCF (= 영업현금흐름 − Capex) 연간 합산. 2020~현재. 신뢰도 100%.<br>'
        '• <span style="color:#cdd6f4">점선 (추정값)</span> : 최근 TTM 성장률 단순 연장 + 공식 Capex 가이던스 반영. '
        '애널리스트 컨센서스가 아닌 역사적 추세 기반으로 실제와 상이할 수 있음.<br>'
        f'• <span style="color:#f9e2af">2026 Capex 가이던스</span> ({HYPSCALER_GUIDANCE_NOTE}) : '
        f'MSFT ${HYPSCALER_GUIDANCE["MSFT"]}B · '
        f'GOOGL ${HYPSCALER_GUIDANCE["GOOGL"]}B · '
        f'AMZN ${HYPSCALER_GUIDANCE["AMZN"]}B · '
        f'META ${HYPSCALER_GUIDANCE["META"]}B = '
        f'<span style="color:#f38ba8;font-weight:600;">합산 ${HYPSCALER_GUIDANCE_TOTAL}B</span><br>'
        '• 가이던스 수치는 분기 실적발표마다 변동될 수 있으며, 최신 공시 확인 후 수동 업데이트 필요. '
        '투자 판단의 참고 자료로만 활용할 것.'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── 가이던스 상세 expander ────────────────────────────────────
    _SRC = {
        'MSFT':  'Q3 FY2026 실적발표 2026.04.30 (MS 회계연도 기준)',
        'GOOGL': 'Q1 2026 실적발표 2026.04.29 ($180~190B 중간값)',
        'AMZN':  'Q1 2026 실적발표 2026.05.01',
        'META':  'Q1 2026 실적발표 2026.04.29 ($125~145B 중간값)',
    }
    with st.expander('📋 2026 Capex 가이던스 상세', expanded=False):
        rows = ''.join(
            f'<tr>'
            f'<td style="padding:4px 10px;font-size:0.72rem;color:#cdd6f4">{tk}</td>'
            f'<td style="padding:4px 10px;font-size:0.72rem;color:#f9e2af;font-weight:600">${vl}B</td>'
            f'<td style="padding:4px 10px;font-size:0.7rem;color:#7f849c">{_SRC[tk]}</td>'
            f'</tr>'
            for tk, vl in HYPSCALER_GUIDANCE.items()
        )
        st.markdown(
            f'<table style="width:100%;border-collapse:collapse;margin-top:4px">'
            f'<thead><tr>'
            f'<th style="padding:3px 10px;font-size:0.7rem;color:#6c7086;font-weight:500;text-align:left">종목</th>'
            f'<th style="padding:3px 10px;font-size:0.7rem;color:#6c7086;font-weight:500;text-align:left">2026 Capex</th>'
            f'<th style="padding:3px 10px;font-size:0.7rem;color:#6c7086;font-weight:500;text-align:left">출처</th>'
            f'</tr></thead><tbody>{rows}'
            f'<tr style="border-top:1px solid #313244">'
            f'<td style="padding:5px 10px;font-size:0.72rem;color:#a6adc8;font-weight:600">합계</td>'
            f'<td style="padding:5px 10px;font-size:0.72rem;color:#f38ba8;font-weight:600">${HYPSCALER_GUIDANCE_TOTAL}B</td>'
            f'<td></td></tr></tbody></table>',
            unsafe_allow_html=True,
        )


def clear_cache():
    load_news.clear()
    load_tickers.clear()
    fetch_finnhub_data.clear()
    fetch_premium_fundamentals.clear()
    fetch_fear_greed.clear()
    fetch_fred_data.clear()
    fetch_cape_data.clear()
    fetch_buffett_history.clear()
    fetch_economic_calendar.clear()
    fetch_hyperscaler_data.clear()


def _sort_tickers_by_mcap():
    """등록된 종목을 시가총액 내림차순으로 자동 재정렬한다."""
    try:
        tickers = get_tickers()
        if not tickers or len(tickers) <= 1:
            return
        api_key = os.environ.get('FINNHUB_API_KEY', '')
        if not api_key:
            return
        H_s = {'User-Agent': 'Mozilla/5.0'}
        BASE_s = 'https://finnhub.io/api/v1'
        def _get_mcap(sym):
            try:
                r = requests.get(
                    f'{BASE_s}/stock/metric?symbol={sym}&metric=all&token={api_key}',
                    headers=H_s, timeout=6)
                if r.ok:
                    return r.json().get('metric', {}).get('marketCapitalization') or 0
            except Exception:
                pass
            return 0
        sorted_tickers = sorted(tickers, key=lambda x: _get_mcap(x['ticker']), reverse=True)
        reorder_tickers(sorted_tickers)
    except Exception as e:
        print(f'[mcap_sort] {e}')


def render_sentiment_card(ticker_sym, sentiment_df):
    """
    Finnhub 감성 데이터 카드 렌더링.
    sentiment_df: load_sentiment_data() 반환값 (전체 SENTIMENT 시트)
    데이터 없으면 카드 미표시.
    """
    if sentiment_df is None or sentiment_df.empty:
        return

    if 'ticker' not in sentiment_df.columns:
        return

    # 해당 종목 최신 행 추출
    t_df = sentiment_df[sentiment_df['ticker'] == ticker_sym.upper()]
    if t_df.empty:
        return

    # date 컬럼 기준 최신 행
    if 'date' in t_df.columns:
        t_df = t_df.sort_values('date', ascending=False)
    row = t_df.iloc[0]

    def _pct(val):
        """0~1 float → '72%' 문자열, 없으면 '—'"""
        try:
            v = float(val)
            return f'{v * 100:.0f}%'
        except (TypeError, ValueError):
            return '—'

    def _int(val):
        try:
            return f'{int(float(val)):,}'
        except (TypeError, ValueError):
            return '—'

    def _float(val):
        try:
            return f'{float(val):.2f}'
        except (TypeError, ValueError):
            return '—'

    news_bull = _pct(row.get('news_bull_pct'))
    news_bear = _pct(row.get('news_bear_pct'))
    news_buzz = _float(row.get('news_buzz'))

    reddit_pos     = _pct(row.get('reddit_pos'))
    reddit_neg     = _pct(row.get('reddit_neg'))
    reddit_mention = _int(row.get('reddit_mention'))

    twitter_pos     = _pct(row.get('twitter_pos'))
    twitter_neg     = _pct(row.get('twitter_neg'))
    twitter_mention = _int(row.get('twitter_mention'))

    collected = str(row.get('collected_at', ''))[:16]
    date_str  = str(row.get('date', ''))

    # 뉴스 감성 색상
    try:
        bull_v = float(row.get('news_bull_pct') or 0)
        news_color = '#a6e3a1' if bull_v >= 0.55 else ('#f38ba8' if bull_v <= 0.40 else '#f9e2af')
    except (TypeError, ValueError):
        news_color = '#cdd6f4'

    with st.expander('📊 감성 분석', expanded=False):
        st.markdown(
            f'<div style="font-size:0.7rem;color:#585b70;margin-bottom:8px;">'
            f'기준일: {date_str} &nbsp;|&nbsp; 수집: {collected}</div>',
            unsafe_allow_html=True
        )

        # ── 뉴스 감성 ──────────────────────────────────────────
        st.markdown(
            '<div style="font-size:0.75rem;color:#a6adc8;margin:6px 0 4px 0;">'
            '📰 뉴스 감성 (Finnhub)</div>',
            unsafe_allow_html=True
        )
        st.markdown(
            f'<div class="fin-grid">'
            f'<div class="fin-chip"><div class="fc-label">Bullish</div>'
            f'<div class="fc-value" style="color:{news_color};">{news_bull}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">Bearish</div>'
            f'<div class="fc-value">{news_bear}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">Buzz 지수</div>'
            f'<div class="fc-value">{news_buzz}</div></div>'
            f'</div>',
            unsafe_allow_html=True
        )

        # ── Reddit 감성 ─────────────────────────────────────────
        if reddit_mention != '—':
            st.markdown(
                '<div style="font-size:0.75rem;color:#a6adc8;margin:6px 0 4px 0;">'
                '🔴 Reddit</div>',
                unsafe_allow_html=True
            )
            st.markdown(
                f'<div class="fin-grid">'
                f'<div class="fin-chip"><div class="fc-label">긍정</div>'
                f'<div class="fc-value positive">{reddit_pos}</div></div>'
                f'<div class="fin-chip"><div class="fc-label">부정</div>'
                f'<div class="fc-value negative">{reddit_neg}</div></div>'
                f'<div class="fin-chip"><div class="fc-label">멘션 수</div>'
                f'<div class="fc-value">{reddit_mention}</div></div>'
                f'</div>',
                unsafe_allow_html=True
            )

        # ── Twitter/X 감성 ──────────────────────────────────────
        if twitter_mention != '—':
            st.markdown(
                '<div style="font-size:0.75rem;color:#a6adc8;margin:6px 0 4px 0;">'
                '🐦 Twitter / X</div>',
                unsafe_allow_html=True
            )
            st.markdown(
                f'<div class="fin-grid">'
                f'<div class="fin-chip"><div class="fc-label">긍정</div>'
                f'<div class="fc-value positive">{twitter_pos}</div></div>'
                f'<div class="fin-chip"><div class="fc-label">부정</div>'
                f'<div class="fc-value negative">{twitter_neg}</div></div>'
                f'<div class="fin-chip"><div class="fc-label">멘션 수</div>'
                f'<div class="fc-value">{twitter_mention}</div></div>'
                f'</div>',
                unsafe_allow_html=True
            )


def render_ticker_content(ticker_sym, ticker_df, tab_idx=0):
    """종목별 브리핑 + 기사 카드 + 페이지네이션 렌더링"""
    no_news = ticker_df.empty

    if not no_news:
        if 'collected_at' in ticker_df.columns:
            ticker_df = ticker_df.sort_values('collected_at', ascending=False)
        company_name = ticker_df['company'].iloc[0] if 'company' in ticker_df.columns else ticker_sym
        total = len(ticker_df)
    else:
        company_name = ticker_sym
        total = 0

    st.markdown(
        f'<div style="color:#6c7086;font-size:0.85rem;margin-bottom:12px;">'
        f'<span class="ticker-badge">{ticker_sym}</span>{company_name}'
        f'{f" · {total}건" if total > 0 else " · 오늘 수집된 기사 없음"}</div>',
        unsafe_allow_html=True
    )

    # ── 안 A + 안 B: 가격·지표 카드 + 상세 expander ────────────
    # 기사 유무와 무관하게 항상 표시
    fin_data = fetch_finnhub_data(ticker_sym)

    # 업종 베타·기준일시 등을 주가정보 카드에도 전달하기 위해 먼저 fetch
    if PREMIUM_UNLOCKED:
        _price  = float(fin_data.get('current') or fin_data.get('prev_close') or 0)
        _mcap   = float(fin_data.get('mcap') or 0)
        _ind_ov = st.session_state.get('ind_override_' + ticker_sym, '')
        fundamentals = fetch_premium_fundamentals(ticker_sym, _price, _mcap, _ind_ov)
    else:
        fundamentals = None

    render_stock_header(ticker_sym, fin_data, fundamentals=fundamentals)

    # ── 프리미엄 분석 섹션 ──────────────────────────────────────
    render_premium_analysis(ticker_sym, fundamentals=fundamentals if fundamentals else None)

    # ── 감성 분석 카드 ──────────────────────────────────────────
    _sentiment_df = load_sentiment_data()
    render_sentiment_card(ticker_sym, _sentiment_df)

    # ── 종합 브리핑 박스 ─────────────────────────────────────────
    summary_kr          = ''
    summary_collected_at = ''
    summary_from_archive = False

    # 1) 오늘 TODAY 시트에서 summary_kr 탐색
    if not no_news and 'summary_kr' in ticker_df.columns:
        for _i, _val in enumerate(ticker_df['summary_kr']):
            if _val and str(_val).strip():
                summary_kr = str(_val).strip()
                # 같은 행의 collected_at 가져오기
                if 'collected_at' in ticker_df.columns:
                    _cat = ticker_df['collected_at'].iloc[_i]
                    if _cat:
                        summary_collected_at = str(_cat).strip()
                break

    # 2) 없으면 아카이브 시트에서 최근 summary 로드 (1시간 캐시)
    if not summary_kr:
        _arch = get_archive_summary_cached(ticker_sym)
        if _arch and _arch.get('summary_kr'):
            summary_kr           = _arch['summary_kr']
            summary_collected_at = _arch.get('collected_at', '')
            summary_from_archive = True

    # 3) 브리핑 렌더링
    if summary_kr:
        body_html = format_summary_html(summary_kr)
        # 업데이트 시점 계산
        if summary_from_archive and summary_collected_at:
            try:
                _cat_dt   = datetime.strptime(summary_collected_at, '%Y-%m-%d %H:%M:%S')
                _cat_dt   = KST.localize(_cat_dt)
                _days_ago = (datetime.now(KST) - _cat_dt).days
                _ago_str  = f'{_days_ago}일 전 업데이트' if _days_ago > 0 else '오늘 업데이트'
            except Exception:
                _ago_str = '이전 업데이트'
            _date_display = f'🕐 {_ago_str}'
        else:
            _date_display = f'📅 {et_date_str()}'
        st.markdown(
            f'<div class="brief-box">'
            f'<div class="brief-title">📋 최근 {ticker_sym} 뉴스 종합 브리핑</div>'
            f'<div class="brief-time">{_date_display}</div>'
            f'{body_html}'
            f'</div>',
            unsafe_allow_html=True
        )

    # 기사 없으면 안내 후 종료
    if no_news:
        if not summary_kr:
            st.info("📭 오늘 수집된 기사가 없습니다. 다음 수집 주기를 기다려주세요.")
        return

    # 페이지네이션
    ITEMS_PER_PAGE = 10
    page_key = f"page_{ticker_sym}"
    if 'page' not in st.session_state:
        st.session_state['page'] = {}
    if page_key not in st.session_state['page']:
        st.session_state['page'][page_key] = 0

    current_page = st.session_state['page'][page_key]
    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    start = current_page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_df = ticker_df.iloc[start:end]

    # 기사 카드
    for idx, (_, row) in enumerate(page_df.iterrows(), start=start + 1):
        title_en = row.get('title', '(제목 없음)')
        title_kr = row.get('title_kr', '') or title_en
        link = row.get('link', '#')
        published = row.get('published', '')
        collected = row.get('collected_at', '')
        article_summary = str(row.get('article_summary_kr', '') or '')

        # [본문] / [AI추론] 마커 파싱 (구 데이터 호환)
        badge_html = ''
        if article_summary.startswith('[본문] '):
            badge_html = '<div><span class="badge-crawled">📄 본문 기반</span></div>'
            article_summary = article_summary[len('[본문] '):]
        elif article_summary.startswith('[AI추론] '):
            badge_html = '<div><span class="badge-inferred">🔍 AI 추론</span></div>'
            article_summary = article_summary[len('[AI추론] '):]

        meta_parts = []
        if published:
            meta_parts.append(f"📅 {published[:25]}")
        if collected:
            meta_parts.append(f"수집: {collected[:16]}")
        meta = " &nbsp;|&nbsp; ".join(meta_parts)

        summary_html = (
            f'{badge_html}<div class="news-summary">{article_summary}</div>'
            if article_summary.strip() else ''
        )

        st.markdown(
            f'<div class="news-card">'
            f'<span class="news-index">#{idx}</span>'
            f'<div class="news-title-kr"><a href="{link}" target="_blank">{title_kr}</a></div>'
            f'<div class="news-title-en">{title_en}</div>'
            f'{summary_html}'
            f'<div class="news-meta">{meta}</div>'
            f'</div>',
            unsafe_allow_html=True
        )

    # 페이지 버튼
    if total_pages > 1:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col1:
            if current_page > 0:
                if st.button("◀ 이전", key=f"prev_{ticker_sym}_{tab_idx}"):
                    st.session_state['page'][page_key] -= 1
                    st.rerun()
        with col2:
            st.markdown(
                f'<div style="text-align:center;color:#6c7086;font-size:0.85rem;padding-top:8px;">'
                f'{current_page + 1} / {total_pages} 페이지</div>',
                unsafe_allow_html=True
            )
        with col3:
            if current_page < total_pages - 1:
                if st.button("다음 ▶", key=f"next_{ticker_sym}_{tab_idx}"):
                    st.session_state['page'][page_key] += 1
                    st.rerun()


# ── 페이지 설정 + CSS 주입 ───────────────────────────────────────
st.set_page_config(page_title='ValueHunter', page_icon='🎯', layout='wide')

st.markdown("""
<style>
/* ── 칩 그리드 ── */
.fin-grid {
    display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0;
}
.fin-chip {
    background: #1e1e2e; border: 1px solid #313244; border-radius: 8px;
    padding: 8px 12px; min-width: 110px; flex: 1;
}
.fc-label { font-size: 0.72rem; color: #a6adc8; margin-bottom: 2px; }
.fc-value { font-size: 1rem; font-weight: 600; color: #cdd6f4; }
.fc-value.up { color: #a6e3a1; }
.fc-value.down { color: #f38ba8; }
.positive { color: #a6e3a1; }
.negative { color: #f38ba8; }

/* ── 투자 분석 카드 ── */
.analysis-card {
    background: #1e1e2e; border: 1px solid #313244; border-radius: 10px;
    padding: 16px; margin: 8px 0;
}
.analysis-card-title {
    font-size: 0.95rem; font-weight: 600; color: #cdd6f4; margin-bottom: 12px;
}
.analysis-metric-row {
    display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px;
}
.analysis-chip {
    background: #181825; border: 1px solid #45475a; border-radius: 6px;
    padding: 6px 10px; min-width: 90px;
}
.chip-label { font-size: 0.68rem; color: #a6adc8; margin-bottom: 2px; }
.chip-value { font-size: 0.9rem; font-weight: 600; color: #cdd6f4; }
.chip-value.positive { color: #a6e3a1; }
.chip-value.negative { color: #f38ba8; }

/* ── 판정 뱃지 ── */
.analysis-verdict {
    border-radius: 6px; padding: 8px 12px;
    font-size: 0.85rem; font-weight: 500; margin: 8px 0;
}
.verdict-buy  { background: #1a2e1a; color: #a6e3a1; }
.verdict-watch{ background: #2e2a1a; color: #f9e2af; }
.verdict-pass { background: #2e1a1a; color: #f38ba8; }
.verdict-wait { background: #1e1e2e; color: #a6adc8; border: 1px solid #313244; }
.analysis-hint-details summary {
    font-size: 0.82rem; color: #89b4fa; cursor: pointer; margin-top: 6px; font-weight: 500;
}
.analysis-hint { font-size: 0.85rem; color: #a6adc8; margin-top: 6px; line-height: 1.6; }

/* ── 업종 배지 ── */
.industry-badge {
    display: inline-block; background: #313244; color: #a6adc8;
    border-radius: 6px; padding: 3px 10px; font-size: 0.78rem; margin-bottom: 8px;
}
.industry-badge.overridden {
    background: #2a2a45; color: #89b4fa; border: 1px solid #45475a;
}

/* ── Rule of 40 바 ── */
.r40-bar-wrap {
    background: #313244; border-radius: 4px; height: 8px;
    width: 100%; margin: 6px 0; overflow: hidden;
}
.r40-bar-fill { height: 8px; border-radius: 4px; }
.r40-bar-fill.pass  { background: #a6e3a1; }
.r40-bar-fill.watch { background: #f9e2af; }
.r40-bar-fill.fail  { background: #f38ba8; }

/* ── 뉴스 카드 ── */
.news-card {
    background: #1e1e2e; border: 1px solid #313244; border-radius: 10px;
    padding: 14px 16px; margin: 8px 0;
}
.news-index { font-size: 0.72rem; color: #7f849c; }
.news-title-kr { font-size: 0.95rem; font-weight: 600; color: #cdd6f4; margin: 4px 0; }
.news-title-kr a { color: #89b4fa; text-decoration: none; }
.news-title-kr a:hover { text-decoration: underline; }
.news-title-en { font-size: 0.78rem; color: #7f849c; margin-bottom: 6px; }
.news-summary { font-size: 0.82rem; color: #a6adc8; line-height: 1.55; margin: 6px 0; }
.news-meta { font-size: 0.72rem; color: #7f849c; margin-top: 8px; }
.badge-inferred {
    background: #2a2a45; color: #89b4fa; border-radius: 4px;
    padding: 1px 8px; font-size: 0.72rem;
}

/* ── 프리미엄 잠금 ── */
.premium-lock-card {
    background: #1e1e2e; border: 1px dashed #45475a; border-radius: 10px;
    padding: 16px; margin: 8px 0; display: flex; align-items: center; gap: 14px;
}
.premium-lock-icon { font-size: 1.6rem; }
.premium-lock-title { font-size: 0.9rem; font-weight: 600; color: #cdd6f4; }
.premium-lock-desc { font-size: 0.78rem; color: #7f849c; margin-top: 3px; }
.premium-lock-badge {
    margin-left: auto; background: #313244; color: #a6adc8;
    border-radius: 6px; padding: 3px 10px; font-size: 0.75rem;
}

.expander-section-label {
    font-size: 0.75rem; font-weight: 600; color: #7f849c;
    text-transform: uppercase; letter-spacing: 0.05em; margin: 10px 0 4px 0;
}
.rec-bar { display: flex; border-radius: 4px; overflow: hidden; height: 10px; margin: 6px 0; }
.rec-segment { height: 10px; }
.rec-legend { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.rec-label { font-size: 0.72rem; color: #a6adc8; display: flex; align-items: center; gap: 4px; }
.rec-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }

/* ── 기업 유형 배지 (복합기업 / 고성장) ── */
.entity-badge {
    display: inline-block; border-radius: 6px; padding: 2px 10px;
    font-size: 0.78rem; font-weight: 600;
}
.entity-badge.conglom {
    background: #2a2045; color: #cba6f7; border: 1px solid #6c5fa6;
}
.entity-badge.highgrowth {
    background: #1a2e3a; color: #89dceb; border: 1px solid #3a6a7a;
}

/* ── 매크로 지표 툴팁 ── */
.macro-tip {
    position: relative; display: inline-block;
    cursor: help; margin-left: 3px;
    font-size: 0.65rem; color: #45475a; vertical-align: middle;
}
.macro-tip .tip-box {
    visibility: hidden; opacity: 0;
    width: 210px;
    background: #1e1e2e; border: 1px solid #45475a;
    color: #cdd6f4; border-radius: 7px;
    padding: 8px 10px; font-size: 0.67rem; line-height: 1.5;
    position: absolute; left: 0; top: 130%;
    z-index: 9999; transition: opacity 0.15s;
    pointer-events: none; font-weight: 400;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
}
.macro-tip:hover .tip-box { visibility: visible; opacity: 1; }

/* ── 사이드바 매크로 차트 expander 다크 스타일 ── */
section[data-testid="stSidebar"] [data-testid="stExpander"] {
    margin: 2px 0 !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] details {
    border: 1px solid #313244 !important;
    border-radius: 8px !important;
    background: #1e1e2e !important;
    overflow: hidden !important;
    padding: 0 !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] details summary {
    padding: 6px 12px !important;
    cursor: pointer !important;
    user-select: none !important;
    border-bottom: none !important;
    background: transparent !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] details[open] summary {
    border-bottom: 1px solid #313244 !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] details summary span,
section[data-testid="stSidebar"] [data-testid="stExpander"] details summary p {
    font-size: 0.73rem !important;
    color: #cdd6f4 !important;
    font-weight: 400 !important;
    line-height: 1.4 !important;
    margin: 0 !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] details summary svg {
    color: #7f849c !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] details > div {
    background: #1e1e2e !important;
    padding: 4px 8px 8px 8px !important;
}

/* ── 분석카드 서브타이틀 ── */
.analysis-card-subtitle {
    font-size: 0.75rem; color: #7f849c; margin-bottom: 10px; margin-top: -4px;
}

/* ── 경고 메시지 ── */
.analysis-warn {
    background: #2e2a1a; border-left: 3px solid #f9e2af;
    border-radius: 4px; padding: 6px 10px;
    font-size: 0.78rem; color: #f9e2af; margin: 6px 0;
}

/* ── Scenario DCF ── */
.scenario-row { display: flex; gap: 8px; margin: 10px 0; flex-wrap: wrap; }
.scenario-chip { flex: 1; min-width: 90px; border-radius: 8px; padding: 10px 12px; text-align: center; }
.scenario-chip.bear { background: #2e1a1a; border: 1px solid #f38ba8; }
.scenario-chip.base { background: #1e1e2e; border: 1px solid #45475a; }
.scenario-chip.bull { background: #1a2e1a; border: 1px solid #a6e3a1; }
.sc-label { font-size: 0.78rem; color: #a6adc8; margin-bottom: 4px; font-weight: 600; }
.sc-value { font-size: 1.05rem; font-weight: 700; color: #cdd6f4; }
.sc-marker { font-size: 0.72rem; margin-top: 4px; }
.sc-up { color: #a6e3a1; }
.sc-down { color: #f38ba8; }

/* ── 애널리스트 바 (rec-bar-wrap 별칭) ── */
.rec-bar-wrap { display: flex; border-radius: 4px; overflow: hidden; height: 10px; margin: 6px 0; }

/* ── 탭 인덱스 색상 (책 인덱스 스타일) ── */
/* nth-child(1) = 하이퍼스케일러 탭, 2~11 = 종목 탭 */
[data-baseweb="tab-list"] button:nth-child(1)  { color: #cba6f7 !important; border-bottom-color: #cba6f7 !important; }
[data-baseweb="tab-list"] button:nth-child(2)  { color: #89b4fa !important; border-bottom-color: #89b4fa !important; }
[data-baseweb="tab-list"] button:nth-child(3)  { color: #a6e3a1 !important; border-bottom-color: #a6e3a1 !important; }
[data-baseweb="tab-list"] button:nth-child(4)  { color: #fab387 !important; border-bottom-color: #fab387 !important; }
[data-baseweb="tab-list"] button:nth-child(5)  { color: #f9e2af !important; border-bottom-color: #f9e2af !important; }
[data-baseweb="tab-list"] button:nth-child(6)  { color: #cba6f7 !important; border-bottom-color: #cba6f7 !important; }
[data-baseweb="tab-list"] button:nth-child(7)  { color: #94e2d5 !important; border-bottom-color: #94e2d5 !important; }
[data-baseweb="tab-list"] button:nth-child(8)  { color: #eba0ac !important; border-bottom-color: #eba0ac !important; }
[data-baseweb="tab-list"] button:nth-child(9)  { color: #89dceb !important; border-bottom-color: #89dceb !important; }
[data-baseweb="tab-list"] button:nth-child(10) { color: #b5e8b0 !important; border-bottom-color: #b5e8b0 !important; }
[data-baseweb="tab-list"] button:nth-child(11) { color: #f2cdcd !important; border-bottom-color: #f2cdcd !important; }
/* 선택된 탭 강조 */
[data-baseweb="tab-list"] button[aria-selected="true"] {
    font-weight: 700 !important; opacity: 1 !important;
}
[data-baseweb="tab-list"] button[aria-selected="false"] {
    opacity: 0.6 !important;
}
/* 날짜·보조 텍스트 칩 */
.fc-date {
    font-size: 0.65rem; color: #7f849c; font-weight: 400;
    margin-left: 6px; vertical-align: middle;
}
/* brief-section 개선 */
.brief-section {
    font-size: 0.88rem !important; font-weight: 700 !important;
    margin: 14px 0 6px 0 !important; padding: 5px 12px !important;
    border-radius: 5px !important; background: rgba(255,255,255,0.04) !important;
    display: block !important;
}
.brief-para {
    font-size: 0.85rem !important; color: #cdd6f4 !important;
    line-height: 1.75 !important; margin: 4px 0 10px 0 !important;
}

/* ── 뉴스 종합 브리핑 박스 ── */
.brief-box {
    background: #1e1e2e; border: 1px solid #313244; border-radius: 10px;
    padding: 18px 22px; margin: 12px 0;
}
.brief-title { font-size: 1rem; font-weight: 700; color: #cdd6f4; margin-bottom: 4px; }
.brief-time  { font-size: 0.75rem; color: #7f849c; margin-bottom: 14px; }

/* ── 사이드바 경제캘린더 selectbox 스타일 (매크로지표 톤 맞춤) ── */
section[data-testid="stSidebar"] [data-testid="stExpander"] [data-testid="stSelectbox"] > div > div {
    background: #181825 !important;
    border: 1px solid #313244 !important;
    border-radius: 6px !important;
    min-height: 28px !important;
    padding: 2px 8px !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] [data-testid="stSelectbox"] [data-baseweb="select"] > div {
    background: #181825 !important;
    border: 1px solid #313244 !important;
    border-radius: 6px !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] [data-testid="stSelectbox"] [data-baseweb="select"] span,
section[data-testid="stSidebar"] [data-testid="stExpander"] [data-testid="stSelectbox"] [data-baseweb="select"] div {
    font-size: 0.7rem !important;
    color: #a6adc8 !important;
    font-weight: 400 !important;
    line-height: 1.4 !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] [data-testid="stSelectbox"] [data-baseweb="select"] svg {
    color: #7f849c !important;
    width: 12px !important;
    height: 12px !important;
}
/* 드롭다운 팝오버 */
[data-baseweb="popover"] ul {
    background: #181825 !important;
    border: 1px solid #313244 !important;
    border-radius: 6px !important;
    padding: 2px 0 !important;
}
[data-baseweb="popover"] ul li {
    font-size: 0.7rem !important;
    color: #a6adc8 !important;
    background: #181825 !important;
    padding: 4px 10px !important;
}
[data-baseweb="popover"] ul li:hover,
[data-baseweb="popover"] ul li[aria-selected="true"] {
    background: #313244 !important;
    color: #cdd6f4 !important;
}

</style>
""", unsafe_allow_html=True)

# 사이드바 ─────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ 종목 관리")
    st.caption(f"업데이트: {kst_now_str()}")
    if st.button("🔄 새로고침", use_container_width=True):
        clear_cache()
        st.rerun()
    # ── 종목 추가 ────────────────────────────────────────────────
    st.subheader("+ 종목 추가")
    if "pending_ticker" not in st.session_state:
        st.session_state["pending_ticker"] = ""
    if "pending_name" not in st.session_state:
        st.session_state["pending_name"] = ""
    with st.form("ticker_lookup_form", clear_on_submit=False):
        ticker_input = st.text_input(
            "티커 입력 (예: AAPL, SOXL)",
            max_chars=10,
            value=st.session_state["pending_ticker"]
        )
        lookup_clicked = st.form_submit_button("회사명 조회", use_container_width=True)
    if "duplicate_ticker" not in st.session_state:
        st.session_state["duplicate_ticker"] = ""
    if lookup_clicked:
        t = ticker_input.upper().strip()
        if t:
            existing_tickers = [r['ticker'] for r in load_tickers()]
            if t in existing_tickers:
                st.session_state["duplicate_ticker"] = t   # 인라인 삭제 UI용
                st.session_state["pending_ticker"] = ""
                st.session_state["pending_name"] = ""
            else:
                st.session_state["duplicate_ticker"] = ""  # 중복 상태 초기화
                name = lookup_company_name(t)
                st.session_state["pending_ticker"] = t
                st.session_state["pending_name"] = name
                if name:
                    st.success(f"{t} -> {name}")
                else:
                    st.warning(f"{t}: 회사명을 찾을 수 없습니다.")
    # ── 이미 등록된 티커: 에러 + 인라인 삭제 버튼 (안 B) ──
    _dup = st.session_state.get("duplicate_ticker", "")
    if _dup:
        _col_msg, _col_btn = st.columns([3, 1])
        with _col_msg:
            st.error(f"이미 등록되어 있는 티커명입니다. ({_dup})")
        with _col_btn:
            if st.button("🗑 삭제", key="del_dup_ticker", use_container_width=True):
                try:
                    remove_ticker(_dup)
                    _sort_tickers_by_mcap()
                    clear_cache()
                    st.session_state["duplicate_ticker"] = ""
                    st.success(f"{_dup} 삭제 완료")
                    st.rerun()
                except Exception as e:
                    st.error(f"삭제 실패: {e}")
    if st.session_state.get("pending_ticker") and st.session_state.get("pending_name"):
        t = st.session_state["pending_ticker"]
        name = st.session_state["pending_name"]
        if st.button(f"{t} ({name}) 추가", use_container_width=True, type="primary"):
            try:
                add_ticker(t, name)
                _sort_tickers_by_mcap()
                clear_cache()
                st.session_state["pending_ticker"] = ""
                st.session_state["pending_name"] = ""
                st.success(f"{t} 추가 완료! (시가총액 기준 자동 정렬)")
                st.rerun()
            except Exception as e:
                st.error(f"추가 실패: {e}")
    st.divider()
    # Fear & Greed Index
    fg = fetch_fear_greed()
    if fg:
        score  = fg['score']
        rk     = fg['rating_kr']
        src    = fg['source']
        if score >= 75:   fg_color = '#f38ba8'
        elif score >= 55: fg_color = '#fab387'
        elif score >= 45: fg_color = '#f9e2af'
        elif score >= 25: fg_color = '#94e2d5'
        else:             fg_color = '#89b4fa'
        st.markdown(
            f'<div style="background:#1e1e2e;border:1px solid #313244;border-radius:8px;'
            f'padding:10px 12px;margin:8px 0;">'
            f'<div style="font-size:0.68rem;color:#7f849c;margin-bottom:4px;">공포탐욕지수'
            f'<span class="macro-tip">💡<span class="tip-box">'
            f'<span style="color:#89dceb;font-weight:600">📊 설명</span> : CNN이 산출하는 0~100 시장 심리 지수. 25↓=극도공포, 75↑=극도탐욕. 주가 모멘텀·옵션·채권 수요 등 7개 지표 합산<br>'
            f'<span style="color:#f9e2af;font-weight:600">⚠️ 한계</span> : 단기 심리를 반영해 노이즈가 크고, 추세 반전 시점을 정확히 포착하기 어려움<br>'
            f'<span style="color:#a6e3a1;font-weight:600">🎯 활용</span> : 극도공포(25↓) 구간은 역발상 매수 기회, 극도탐욕(75↑) 구간은 비중 축소 신호로 참고'
            f'</span></span>'
            f'</div>'
            f'<div style="display:flex;align-items:center;gap:8px;">'
            f'<span style="font-size:1.4rem;font-weight:700;color:{fg_color};">{score}</span>'
            f'<span style="font-size:0.8rem;color:{fg_color};">{rk}</span>'
            f'</div>'
            f'<div style="background:#313244;border-radius:3px;height:4px;margin-top:6px;">'
            f'<div style="background:{fg_color};width:{score}%;height:4px;border-radius:3px;"></div>'
            f'</div>'
            f'<div style="font-size:0.62rem;color:#45475a;margin-top:4px;">출처: {src}</div>'
            f'</div>',
            unsafe_allow_html=True
        )
    # ── 매크로 지표 ──────────────────────────────────────────────
    fred_data  = fetch_fred_data()
    cape_info  = fetch_cape_data()
    cape_curr  = cape_info.get('current')
    if fred_data or cape_curr is not None:
        st.markdown(
            '<div style="font-size:0.72rem;color:#7f849c;margin:10px 0 2px 0;'
            'font-weight:600;">📊 시장 매크로 지표(고평가 여부 확인)</div>',
            unsafe_allow_html=True)
        # ── 행별 렌더링 헬퍼 (각 지표를 독립 박스로 출력) ───────────
        def _rrow(label, value, color='#cdd6f4', sub='', tip=''):
            st.markdown(
                f'<div style="background:#1e1e2e;border:1px solid #313244;'
                f'border-radius:8px;padding:8px 12px;margin:2px 0;">'
                f'{_macro_row(label, value, color, sub, tip)}'
                f'</div>',
                unsafe_allow_html=True)

        import pandas as _pd
        import plotly.graph_objects as _go

        # ── 버핏지수 + 차트 ──────────────────────────────────────
        if 'buffett' in fred_data:
            b  = fred_data['buffett']['value']
            bd = fred_data['buffett']['date']
            if b > 175:   bc, bl, be = '#f38ba8', '극도과열', '🔴'
            elif b > 125: bc, bl, be = '#fab387', '과열',   '🟠'
            elif b > 100: bc, bl, be = '#f9e2af', '주의',   '🟡'
            elif b > 75:  bc, bl, be = '#a6e3a1', '보통',   '🟢'
            else:         bc, bl, be = '#89b4fa', '저평가', '🔵'
            # 지표 행은 _rrow() 로 표시 → 다른 행과 동일한 폰트/색상
            _rrow('버핏지수', f'{b:.1f}%  {be} {bl}', bc, bd,
                  tip='<span style="color:#89dceb;font-weight:600">📊 설명</span> : 미국 주식 시총 ÷ GDP 비율. 100%=공정가치, 125%↑=과열, 175%↑=극도과열<br><span style="color:#f9e2af;font-weight:600">⚠️ 한계</span> : 저금리·QE 환경에서 고수치가 수년간 지속 가능. 단기 타이밍 예측 불가<br><span style="color:#a6e3a1;font-weight:600">🎯 활용</span> : 장기 투자 밸류에이션 참고 및 포트폴리오 비중 조절 기준으로 활용')
            # 차트는 별도 expander (라벨은 아이콘+텍스트만, 폰트 영향 최소화)
            buffett_hist = fetch_buffett_history()
            if buffett_hist:
                with st.expander('📈 버핏지수 히스토리', expanded=False):
                    df_b = _pd.DataFrame(buffett_hist, columns=['quarter', '버핏지수'])
                    _fig_b = _go.Figure()
                    _fig_b.add_trace(_go.Scatter(
                        x=df_b['quarter'], y=df_b['버핏지수'],
                        mode='lines', line=dict(color='#89b4fa', width=1.5)))
                    _fig_b.add_hline(
                        y=100, line_dash='dash',
                        line_color='#f9e2af', line_width=1,
                        annotation_text='100% 기준',
                        annotation_font_color='#f9e2af',
                        annotation_font_size=10)
                    _fig_b.add_trace(_go.Scatter(
                        x=[df_b['quarter'].iloc[-1]], y=[b],
                        mode='markers',
                        marker=dict(color='#f38ba8', size=7)))
                    _all_q = df_b['quarter'].tolist()
                    _step  = max(1, len(_all_q) // 20)
                    _fig_b.update_layout(
                        height=200, showlegend=False,
                        margin=dict(l=30, r=10, t=8, b=30),
                        plot_bgcolor='rgba(0,0,0,0)',
                        paper_bgcolor='rgba(0,0,0,0)',
                        font=dict(color='#cdd6f4', size=10),
                        xaxis=dict(gridcolor='#313244', showgrid=True,
                                   tickvals=_all_q[::_step],
                                   ticktext=[q[:4] for q in _all_q[::_step]]),
                        yaxis=dict(gridcolor='#313244', showgrid=True,
                                   ticksuffix='%'))
                    st.plotly_chart(_fig_b, use_container_width=True,
                                    config={'displayModeBar': False})
                    st.caption('출처: Yahoo Finance ^W5000 / FRED GDP')

        else:
            _rrow('버핏지수', '🔄 데이터 일시 불가', '#585b70')

        # ── Shiller CAPE + 인라인 차트 ───────────────────────────
        if cape_curr is not None:
            if cape_curr > 40:   cc2, ce, cl = '#f38ba8', '🔴', '극도과열'
            elif cape_curr > 30: cc2, ce, cl = '#fab387', '🟠', '과열'
            elif cape_curr > 20: cc2, ce, cl = '#f9e2af', '🟡', '주의'
            else:                cc2, ce, cl = '#a6e3a1', '🟢', '저평가'
            cape_hist_now = cape_info.get('history', [])
            # 역사평균: 값이 50 미만인 것만 유효한 CAPE 값으로 판단
            valid_hist = [(y, v) for y, v in cape_hist_now if v < 200]
            if valid_hist:
                hist_avg = sum(v for _, v in valid_hist) / len(valid_hist)
                ratio    = cape_curr / hist_avg
                cape_sub = f'역사평균 {hist_avg:.1f} ({ratio:.1f}x)'
            else:
                hist_avg = 17.0
                ratio    = cape_curr / hist_avg
                cape_sub = f'역사평균 ~{hist_avg:.0f} ({ratio:.1f}x)'
            # 차트용 cape_hist: 필터 없이 원본 사용 (차트는 항상 표시)
            cape_hist = cape_info.get('history', [])
            # 지표 행은 _rrow() 로 표시
            _cape_date = cape_info.get('date', '')
            _cape_sub_full = f'{cape_sub}  {_cape_date}' if _cape_date else cape_sub
            _rrow('Shiller CAPE', f'{cape_curr:.1f}  {ce} {cl}', cc2, _cape_sub_full,
                  tip='<span style="color:#89dceb;font-weight:600">📊 설명</span> : 물가조정 주가순이익비율(10년 평균 EPS). 역사 평균 ~17, 30↑=과열, 40↑=극도과열<br><span style="color:#f9e2af;font-weight:600">⚠️ 한계</span> : 단기 타이밍 예측 부적합. 과열 상태가 수년간 지속 가능<br><span style="color:#a6e3a1;font-weight:600">🎯 활용</span> : 장기 기대수익률 추정 및 분할매수 전략의 밸류에이션 기준으로 활용')
            if cape_hist:
                with st.expander('📈 Shiller CAPE 히스토리', expanded=False):
                    df_c = _pd.DataFrame(cape_hist, columns=['year', 'CAPE'])
                    df_c = df_c.sort_values('year')
                    _cape_mean = round(sum(v for _, v in cape_hist) / len(cape_hist), 1)
                    _fig_c = _go.Figure()
                    _fig_c.add_trace(_go.Scatter(
                        x=df_c['year'], y=df_c['CAPE'],
                        mode='lines', line=dict(color='#89b4fa', width=1.5)))
                    _fig_c.add_hline(
                        y=_cape_mean, line_dash='dash',
                        line_color='#f9e2af', line_width=1,
                        annotation_text=f'평균 {_cape_mean}',
                        annotation_font_color='#f9e2af',
                        annotation_font_size=10)
                    _fig_c.add_trace(_go.Scatter(
                        x=[df_c['year'].iloc[-1]], y=[cape_curr],
                        mode='markers',
                        marker=dict(color='#f38ba8', size=7)))
                    _fig_c.update_layout(
                        height=200, showlegend=False,
                        margin=dict(l=30, r=10, t=8, b=30),
                        plot_bgcolor='rgba(0,0,0,0)',
                        paper_bgcolor='rgba(0,0,0,0)',
                        font=dict(color='#cdd6f4', size=10),
                        xaxis=dict(gridcolor='#313244', showgrid=True),
                        yaxis=dict(gridcolor='#313244', showgrid=True))
                    st.plotly_chart(_fig_c, use_container_width=True,
                                    config={'displayModeBar': False})
                    st.caption('출처: multpl.com / Robert Shiller')

        # ── 나머지 지표 (하나의 블록) ────────────────────────────
        def _unavail(label):
            return (f'<div style="display:flex;justify-content:space-between;'
                    f'align-items:center;padding:2px 0;">'
                    f'<span style="color:#7f849c;font-size:0.78rem;">{label}</span>'
                    f'<span style="color:#585b70;font-size:0.75rem;">🔄 데이터 일시 불가</span>'
                    f'</div>')

        rest_html = ''
        if 'fed_rate' in fred_data:
            rest_html += _macro_row(
                '기준금리 (FFR)',
                f"{fred_data['fed_rate']['value']:.2f}%",
                '#cdd6f4', fred_data['fed_rate']['date'],
                tip='<span style="color:#89dceb;font-weight:600">📊 설명</span> : 연준(Fed)이 설정하는 단기 기준금리. 인상 시 주식 할인율 상승 → 주가 하방 압력<br><span style="color:#f9e2af;font-weight:600">⚠️ 한계</span> : 시장은 금리 기대치를 미리 선반영. 실제 인상 발표 시 주가가 오히려 상승 가능<br><span style="color:#a6e3a1;font-weight:600">🎯 활용</span> : FOMC 회의 일정과 함께 금리 추세 방향 확인')
        else:
            rest_html += _unavail('기준금리 (FFR)')
        if 't10y' in fred_data:
            rest_html += _macro_row(
                '미국채 10Y', f"{fred_data['t10y']['value']:.2f}%",
                sub=fred_data['t10y']['date'],
                tip='<span style="color:#89dceb;font-weight:600">📊 설명</span> : 미국 10년 만기 국채 수익률. 장기 경기 전망·인플레이션 기대 반영. 상승 시 성장주 압박<br><span style="color:#f9e2af;font-weight:600">⚠️ 한계</span> : 중앙은행 채권 매입(QE)으로 금리가 인위적으로 억제될 수 있음<br><span style="color:#a6e3a1;font-weight:600">🎯 활용</span> : 10Y-2Y 스프레드와 함께 경기 사이클 판단에 활용')
        else:
            rest_html += _unavail('미국채 10Y')
        if 't2y' in fred_data:
            rest_html += _macro_row(
                '미국채 2Y', f"{fred_data['t2y']['value']:.2f}%",
                sub=fred_data['t2y']['date'],
                tip='<span style="color:#89dceb;font-weight:600">📊 설명</span> : 미국 2년 만기 국채 수익률. 단기 금리 전망과 연준 정책 방향을 가장 민감하게 반영<br><span style="color:#f9e2af;font-weight:600">⚠️ 한계</span> : 연준 위원 발언 하나에도 과도하게 반응해 단기 변동성이 큼<br><span style="color:#a6e3a1;font-weight:600">🎯 활용</span> : 10Y와의 차이(장단기 스프레드)로 경기침체 선행 신호 확인')
        else:
            rest_html += _unavail('미국채 2Y')
        if 'spread' in fred_data:
            sp  = fred_data['spread']
            sc2 = '#a6e3a1' if sp >= 0 else '#f38ba8'
            sl2 = '정상' if sp >= 0 else '역전'
            _sp_date = fred_data.get('t10y', {}).get('date', '')
            rest_html += _macro_row(
                '장단기 스프레드', f'{sp:+.2f}%p ({sl2})', sc2,
                sub=_sp_date,
                tip='<span style="color:#89dceb;font-weight:600">📊 설명</span> : 10Y-2Y 국채 금리 차이. 역전(마이너스) 시 과거 8회 중 7회 경기침체 발생한 선행지표<br><span style="color:#f9e2af;font-weight:600">⚠️ 한계</span> : 역전 후 실제 침체까지 6~18개월 시차 존재. 단기 매매 타이밍 도구로 부적합<br><span style="color:#a6e3a1;font-weight:600">🎯 활용</span> : 역전 진입보다 역전 해소 이후 주가 하락 리스크를 더 주의할 것')
        else:
            rest_html += _unavail('장단기 스프레드')
        if 'credit_spread' in fred_data:
            cs  = fred_data['credit_spread']['value']
            cc3 = ('#f38ba8' if cs > 3 else
                   '#fab387' if cs > 2 else '#a6e3a1')
            rest_html += _macro_row('크레딧 스프레드', f'{cs:.2f}%p', cc3,
                sub=fred_data['credit_spread']['date'],
                tip='<span style="color:#89dceb;font-weight:600">📊 설명</span> : 회사채(BBB)-국채 금리 차이. 스프레드 확대 = 기업 신용 위험 상승·시장 공포 신호<br><span style="color:#f9e2af;font-weight:600">⚠️ 한계</span> : 중앙은행의 회사채 직접 매입 등 정책 개입으로 위기 신호가 희석될 수 있음<br><span style="color:#a6e3a1;font-weight:600">🎯 활용</span> : 2%p 초과 시 주의, 3%p 초과 시 위험 신호로 판단')
        else:
            rest_html += _unavail('크레딧 스프레드')
        if 'krw_usd' in fred_data:
            krw = fred_data['krw_usd']['value']
            kd  = fred_data['krw_usd']['date']
            rest_html += _macro_row(
                '원/달러 환율', f'{krw:,.0f} KRW', '#cdd6f4', kd,
                tip='<span style="color:#89dceb;font-weight:600">📊 설명</span> : 1달러 기준 원화 환율. 원화 약세(↑)는 수출주 유리, 수입 물가 상승·외국인 이탈 요인<br><span style="color:#f9e2af;font-weight:600">⚠️ 한계</span> : 단기 변동성이 매우 크고 지정학·금리차·수급 등 복합 요인에 영향받음<br><span style="color:#a6e3a1;font-weight:600">🎯 활용</span> : 1,400원 이상 고환율 지속 시 한국 시장 외국인 수급 악화 주의')
        else:
            rest_html += _unavail('원/달러 환율')
        if 'us_debt' in fred_data and 'krw_usd' in fred_data:
            debt_m     = fred_data['us_debt']['value']
            dd         = fred_data['us_debt']['date']
            krw_r      = fred_data['krw_usd']['value']
            debt_t_usd = debt_m / 1_000_000
            debt_t_krw = debt_m * 1e6 * krw_r / 1e16
            rest_html += _macro_row(
                '미국 국가부채',
                f'${debt_t_usd:.1f}T / {debt_t_krw:.1f}경원',
                '#a6adc8', dd,
                tip='<span style="color:#89dceb;font-weight:600">📊 설명</span> : 미국 연방정부 누적 부채. 절대 금액보다 GDP 대비 비율(현재 약 120%↑)로 해석<br><span style="color:#f9e2af;font-weight:600">⚠️ 한계</span> : 기축통화국 미국은 높은 부채를 장기 유지 가능. 단순 수치 비교는 오해 소지 있음<br><span style="color:#a6e3a1;font-weight:600">🎯 활용</span> : 부채한도 협상 시기(보통 연중) 전후 시장 변동성 확대 주의')
        if rest_html:
            st.markdown(
                f'<div style="background:#1e1e2e;border:1px solid #313244;'
                f'border-radius:8px;padding:10px 12px;margin:4px 0 8px 0;">'
                f'{rest_html}</div>',
                unsafe_allow_html=True)

        render_economic_calendar()




# ── 메인 영역 ────────────────────────────────────────────────────
tickers_list = load_tickers()

_HYPS_TAB = '📊 하이퍼스케일러 분석'

news_df = load_news()
if news_df is None:
    news_df = pd.DataFrame()

if not tickers_list:
    tabs = st.tabs([_HYPS_TAB])
    with tabs[0]:
        render_hyperscaler_tab()
else:
    tab_labels = [_HYPS_TAB] + [t['ticker'] for t in tickers_list]
    tabs = st.tabs(tab_labels)

    with tabs[0]:
        render_hyperscaler_tab()

    for i, (tab, ticker_info) in enumerate(zip(tabs[1:], tickers_list)):
        sym = ticker_info['ticker']
        with tab:
            if not news_df.empty and 'ticker' in news_df.columns:
                ticker_df = news_df[news_df['ticker'] == sym].copy()
            else:
                ticker_df = pd.DataFrame()
            render_ticker_content(sym, ticker_df, tab_idx=i)
