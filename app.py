"""
ValueHunter — 퀀트 기반 가치분석 대시보드
Google Sheets(TODAY 시트)에서 뉴스를 읽어 종목별 탭으로 표시
"""

import re
import streamlit as st
import pandas as pd
import requests
from datetime import datetime
import pytz
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from utils.sheets import get_tickers, add_ticker, remove_ticker, reorder_tickers, get_today_news
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
                'week52h':     m.get('52WeekHigh'),
                'week52l':     m.get('52WeekLow'),
                'mcap':        m.get('marketCapitalization'),
                'net_margin':  m.get('netProfitMarginTTM'),
                'gross_margin':m.get('grossMarginTTM'),
                'rev3y':       m.get('revenueGrowth3Y'),
                'rev5y':       m.get('revenueGrowth5Y'),
                'eps3y':       m.get('epsGrowth3Y'),
                'eps5y':       m.get('epsGrowth5Y'),
            })
        # 3. 목표주가
        r = requests.get(f'{BASE}/stock/price-target?symbol={ticker}&token={api_key}', headers=H, timeout=8)
        if r.ok:
            pt = r.json()
            data.update({
                'target_mean': pt.get('targetMean'),
                'target_high': pt.get('targetHigh'),
                'target_low':  pt.get('targetLow'),
            })
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

    def _fmt_mcap(val):
        if val is None:
            return '—'
        if val >= 1_000_000:
            return f'${val / 1_000_000:.2f}T'
        if val >= 1_000:
            return f'${val / 1_000:.0f}B'
        return f'${val:.0f}M'

    with st.expander('📈 기본 주가정보', expanded=False):
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
        mcap = _fmt_mcap(data.get('mcap'))

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
            f'<div class="fin-chip"><div class="fc-label">52주 고</div><div class="fc-value">{h52}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">52주 저</div><div class="fc-value">{l52}</div></div>'
            f'</div>',
            unsafe_allow_html=True
        )

        # ── 안 A: 핵심지표 6칩 ──────────────────────────────────────
        pe      = _v(data.get('pe'),        '.1f', suf='x')
        roe_v   = data.get('roe')
        roe     = f'{roe_v:.1f}%' if roe_v is not None else '—'
        eps     = _v(data.get('eps'),       '.2f', '$')
        target  = _v(data.get('target_mean'), '.1f', '$')
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
            f'<div class="fin-chip"><div class="fc-label">목표주가 (평균)</div><div class="fc-value">{target}</div></div>'
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
            if shares >= 1e9:
                sh_str = f'{shares / 1e9:.2f}B주'
            elif shares >= 1e6:
                sh_str = f'{shares / 1e6:.0f}M주'
            else:
                sh_str = f'{shares:,.0f}주'
            company_chips.append(('발행주식수', sh_str))
        if mcap_v is not None:
            company_chips.append(('시가총액', _fmt_mcap(mcap_v)))
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
        r3y = _v(data.get('rev3y'),       '.1f', suf='%')
        r5y = _v(data.get('rev5y'),       '.1f', suf='%')
        e3y = _v(data.get('eps3y'),       '.1f', suf='%')
        e5y = _v(data.get('eps5y'),       '.1f', suf='%')
        nm  = _v(data.get('net_margin'),  '.1f', suf='%')
        gm  = _v(data.get('gross_margin'),'.1f', suf='%')

        st.markdown(
            '<div class="expander-section-label">성장률 · 수익성</div>'
            f'<div class="fin-grid">'
            f'<div class="fin-chip"><div class="fc-label">매출성장 3Y</div><div class="fc-value">{r3y}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">매출성장 5Y</div><div class="fc-value">{r5y}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">EPS성장 3Y</div><div class="fc-value">{e3y}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">EPS성장 5Y</div><div class="fc-value">{e5y}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">순이익률</div><div class="fc-value">{nm}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">매출총이익률</div><div class="fc-value">{gm}</div></div>'
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

    with st.expander('🔬 투자 분석 (프리미엄)', expanded=False):

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

    # EDGAR 연동 상태: 오류 시에만 표시
    if PREMIUM_UNLOCKED and fundamentals and fundamentals.get('error'):
        st.warning('⚠️ EDGAR 연동 오류: ' + str(fundamentals['error']))



# ── 유틸 ─────────────────────────────────────────────────────────
def kst_now_str():
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')


@st.cache_data(ttl=300)
def load_news():
    return get_today_news()


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


def clear_cache():
    load_news.clear()
    load_tickers.clear()
    fetch_finnhub_data.clear()
    fetch_premium_fundamentals.clear()
    fetch_fear_greed.clear()


def render_ticker_content(ticker_sym, ticker_df):
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

    # 기사 없으면 안내 후 종료
    if no_news:
        st.info("📭 오늘 수집된 기사가 없습니다. 다음 수집 주기를 기다려주세요.")
        return

    # 종합 브리핑 박스
    summary_kr = ''
    if 'summary_kr' in ticker_df.columns:
        for val in ticker_df['summary_kr']:
            if val and str(val).strip():
                summary_kr = str(val).strip()
                break

    if summary_kr:
        body_html = format_summary_html(summary_kr)
        st.markdown(
            f'<div class="brief-box">'
            f'<div class="brief-title">📋 오늘의 {ticker_sym} 뉴스 종합 브리핑</div>'
            f'<div class="brief-time">📅 {et_date_str()}</div>'
            f'{body_html}'
            f'</div>',
            unsafe_allow_html=True
        )

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
                if st.button("◀ 이전", key=f"prev_{ticker_sym}"):
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
                if st.button("다음 ▶", key=f"next_{ticker_sym}"):
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
[data-baseweb="tab-list"] button:nth-child(1)  { color: #89b4fa !important; border-bottom-color: #89b4fa !important; }
[data-baseweb="tab-list"] button:nth-child(2)  { color: #a6e3a1 !important; border-bottom-color: #a6e3a1 !important; }
[data-baseweb="tab-list"] button:nth-child(3)  { color: #fab387 !important; border-bottom-color: #fab387 !important; }
[data-baseweb="tab-list"] button:nth-child(4)  { color: #f9e2af !important; border-bottom-color: #f9e2af !important; }
[data-baseweb="tab-list"] button:nth-child(5)  { color: #cba6f7 !important; border-bottom-color: #cba6f7 !important; }
[data-baseweb="tab-list"] button:nth-child(6)  { color: #94e2d5 !important; border-bottom-color: #94e2d5 !important; }
[data-baseweb="tab-list"] button:nth-child(7)  { color: #eba0ac !important; border-bottom-color: #eba0ac !important; }
[data-baseweb="tab-list"] button:nth-child(8)  { color: #89dceb !important; border-bottom-color: #89dceb !important; }
[data-baseweb="tab-list"] button:nth-child(9)  { color: #b5e8b0 !important; border-bottom-color: #b5e8b0 !important; }
[data-baseweb="tab-list"] button:nth-child(10) { color: #f2cdcd !important; border-bottom-color: #f2cdcd !important; }
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

</style>
""", unsafe_allow_html=True)

# 사이드바 ─────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ 종목 관리")
    st.caption(f"업데이트: {kst_now_str()}")
    if st.button("🔄 새로고침", use_container_width=True):
        clear_cache()
        st.rerun()
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
            f'<div style="font-size:0.68rem;color:#7f849c;margin-bottom:4px;">공포탐욕지수</div>'
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
    st.divider()
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
    if lookup_clicked:
        t = ticker_input.upper().strip()
        if t:
            name = lookup_company_name(t)
            st.session_state["pending_ticker"] = t
            st.session_state["pending_name"] = name
            if name:
                st.success(f"{t} -> {name}")
            else:
                st.warning(f"{t}: 회사명을 찾을 수 없습니다.")
    if st.session_state.get("pending_ticker") and st.session_state.get("pending_name"):
        t = st.session_state["pending_ticker"]
        name = st.session_state["pending_name"]
        if st.button(f"{t} ({name}) 추가", use_container_width=True, type="primary"):
            try:
                add_ticker(t, name)
                clear_cache()
                st.session_state["pending_ticker"] = ""
                st.session_state["pending_name"] = ""
                st.success(f"{t} 추가 완료!")
                st.rerun()
            except Exception as e:
                st.error(f"추가 실패: {e}")
    st.divider()
    st.subheader("종목 순서 / 삭제")
    tickers_raw = load_tickers()
    if tickers_raw:
        n = len(tickers_raw)
        for i, t in enumerate(tickers_raw):
            sym   = t['ticker']
            cname = t.get('company_name', '')
            col_nm, col_up, col_dn, col_dl = st.columns([3, 1, 1, 1])
            with col_nm:
                st.markdown(
                    f'<div style="padding:5px 0;line-height:1.3;">'
                    f'<span style="font-size:0.85rem;font-weight:600;color:#cdd6f4;">{sym}</span><br>'
                    f'<span style="font-size:0.7rem;color:#7f849c;">{cname}</span></div>',
                    unsafe_allow_html=True
                )
            with col_up:
                if i > 0:
                    if st.button("up", key=f"up_{sym}_{i}", use_container_width=True):
                        new_order = list(tickers_raw)
                        new_order[i], new_order[i - 1] = new_order[i - 1], new_order[i]
                        try:
                            reorder_tickers(new_order)
                            load_tickers.clear()
                            st.rerun()
                        except Exception as e:
                            st.error(f"순서 변경 실패: {e}")
                else:
                    st.markdown('<div style="height:36px;"></div>', unsafe_allow_html=True)
            with col_dn:
                if i < n - 1:
                    if st.button("dn", key=f"dn_{sym}_{i}", use_container_width=True):
                        new_order = list(tickers_raw)
                        new_order[i], new_order[i + 1] = new_order[i + 1], new_order[i]
                        try:
                            reorder_tickers(new_order)
                            load_tickers.clear()
                            st.rerun()
                        except Exception as e:
                            st.error(f"순서 변경 실패: {e}")
                else:
                    st.markdown('<div style="height:36px;"></div>', unsafe_allow_html=True)
            with col_dl:
                if st.button("del", key=f"dl_{sym}_{i}", use_container_width=True):
                    try:
                        remove_ticker(sym)
                        clear_cache()
                        st.rerun()
                    except Exception as e:
                        st.error(f"삭제 실패: {e}")
    else:
        st.info("등록된 종목이 없습니다.")

# 메인
st.title("ValueHunter")
st.caption("퀀트 기반 정량적 가치분석 대시보드 | EDGAR·Damodaran·Finnhub 연동 | 2시간마다 업데이트")
st.divider()
df = load_news()
tickers = load_tickers()
if df.empty or not tickers:
    st.info("아직 수집된 뉴스가 없습니다. GitHub Actions가 2시간마다 뉴스를 수집합니다.")
    st.stop()
ticker_list = tickers if tickers else []
if not ticker_list:
    st.info("사이드바에서 종목을 추가하세요.")
else:
    counts = {}
    if not df.empty and "ticker" in df.columns:
        counts = df.groupby("ticker").size().to_dict()
    tab_labels = []
    for t in ticker_list:
        sym = t["ticker"]
        n = counts.get(sym, 0)
        tab_labels.append(f"{sym} ({n})" if n > 0 else sym)
    tabs = st.tabs(tab_labels)
    for tab, t in zip(tabs, ticker_list):
        sym = t["ticker"]
        with tab:
            if not df.empty and "ticker" in df.columns:
                ticker_df = df[df["ticker"] == sym].copy()
            else:
                ticker_df = pd.DataFrame()
            render_ticker_content(sym, ticker_df)
