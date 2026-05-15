"""
Frank News Dashboard — Streamlit 웹 대시보드
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
from utils.sheets import get_tickers, add_ticker, remove_ticker, get_today_news
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
    summary_kr 텍스트를 섹션별 일관된 HTML로 변환.
    - 라인별 처리로 헤더 직후 빈 줄 항상 제거 (종목별 공백 불일치 해소)
    - 빈 줄은 단락 구분(</p><p>)으로, 연속 줄은 <br>로 처리
    - [핵심 이슈] / [투자 포인트] / [시장 분위기] 헤더 스타일링
    """
    if not text:
        return ''

    SECTION_HEADERS = {'[핵심 이슈]', '[투자 포인트]', '[시장 분위기]'}

    # 연속 빈 줄 정규화
    text = re.sub(r'\n{3,}', '\n\n', text.strip())

    lines = text.split('\n')
    processed = []  # (kind, value): kind = 'header' | 'blank' | 'text'
    skip_empty = False  # 헤더 직후 빈 줄 스킵 플래그

    for line in lines:
        stripped = line.strip()
        if stripped in SECTION_HEADERS:
            processed.append(('header', stripped))
            skip_empty = True          # 헤더 직후 빈 줄 무시 시작
        elif not stripped:
            if not skip_empty:
                processed.append(('blank', ''))
            # skip_empty=True 이면 이 빈 줄을 무시(계속 루프)
        else:
            skip_empty = False         # 실제 내용이 나오면 스킵 해제
            processed.append(('text', stripped))

    # HTML 조합
    html_parts = []
    current_lines = []

    def flush_para():
        if current_lines:
            html_parts.append(
                f'<p class="brief-para">{"<br>".join(current_lines)}</p>'
            )
            current_lines.clear()

    for kind, val in processed:
        if kind == 'header':
            flush_para()
            html_parts.append(f'<p class="brief-section">{val}</p>')
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
    except Exception as e:
        print(f'[Finnhub] {ticker} 수집 오류: {e}')
    return data


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


def render_stock_header(ticker_sym, data):
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

    with st.expander('📈 기본 주가정보', expanded=True):
        # ── 안 A: 가격 4칩 ──────────────────────────────────────────
        chg_pct = data.get('change_pct')
        chg_cls = 'up' if (chg_pct or 0) > 0 else ('down' if (chg_pct or 0) < 0 else '')
        chg_sign = '+' if (chg_pct or 0) > 0 else ''
        chg_str  = f'{chg_sign}{chg_pct:.2f}%' if chg_pct is not None else '—'

        pc   = _v(data.get('prev_close'), '.2f', '$')
        h52  = _v(data.get('week52h'),    '.2f', '$')
        l52  = _v(data.get('week52l'),    '.2f', '$')
        mcap = _fmt_mcap(data.get('mcap'))

        st.markdown(
            f'<div class="fin-grid">'
            f'<div class="fin-chip"><div class="fc-label">전일 종가</div><div class="fc-value">{pc}</div></div>'
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

        st.markdown(
            f'<div class="fin-grid">'
            f'<div class="fin-chip"><div class="fc-label">PER (TTM)</div><div class="fc-value">{pe}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">ROE</div><div class="fc-value">{roe}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">EPS (TTM)</div><div class="fc-value">{eps}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">목표주가 (평균)</div><div class="fc-value">{target}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">배당수익률</div><div class="fc-value">{div}</div></div>'
            f'<div class="fin-chip"><div class="fc-label">베타</div><div class="fc-value">{beta}</div></div>'
            f'</div>',
            unsafe_allow_html=True
        )

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

    with st.expander('🔬 투자 분석 (프리미엄)', expanded=True):

        # 업종 배지 + 선택기
        if PREMIUM_UNLOCKED and fundamentals:
            damod_ind = fundamentals.get('damod_industry', '') or ''
            ind_src   = fundamentals.get('industry_source', 'finnhub_auto')
            is_ov     = ind_src in ('override_user', 'override_auto')
            badge_cls = 'industry-badge overridden' if is_ov else 'industry-badge'
            src_lbl   = ('🔄 수동' if ind_src == 'override_user'
                         else '⚙️ 자동보정' if ind_src == 'override_auto'
                         else '🤖 자동')
            badge_txt = damod_ind if damod_ind else '업종 미매핑'
            st.markdown(
                '<div class="' + badge_cls + '">🏷️ ' + src_lbl + ': ' + badge_txt + '</div>',
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

        # ① ROIC vs WACC
        if PREMIUM_UNLOCKED:
            roic_val   = fundamentals.get('roic')             if fundamentals else None
            wacc_val   = fundamentals.get('wacc_used')        if fundamentals else None
            spread_val = fundamentals.get('roic_wacc_spread') if fundamentals else None
            ind_roic   = fundamentals.get('industry_roic')    if fundamentals else None

            if roic_val is not None and wacc_val is not None:
                spread = spread_val if spread_val is not None else (roic_val - wacc_val)
                sp_cls  = 'positive' if spread > 0 else 'negative'
                sp_sign = '+' if spread > 0 else ''
                v_cls = 'verdict-buy' if spread > 5 else ('verdict-watch' if spread > 0 else 'verdict-pass')
                v_txt = '✅ 가치 창출 (EVA 양수)' if spread > 0 else '⚠️ 자본 파괴 (EVA 음수)'
                hint  = ('ROIC가 WACC 초과 → 경제적 해자 존재, 주주 가치 창출 중.' if spread > 0
                         else 'ROIC가 WACC 미달. 업종 분류 오류 가능성 — 위 업종 선택기로 변경 후 재확인 권장.')
                ind_c = ('<div class="analysis-chip"><div class="chip-label">업종 ROIC</div>'
                         '<div class="chip-value">' + str(round(ind_roic, 1)) + '%</div></div>') if ind_roic is not None else ''
                _ind_tag1 = (' <span style="font-size:0.72rem;font-weight:400;color:#a6adc8;background:#313244;'
                             'border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:middle;">🏷️ '
                             + (fundamentals.get('damod_industry') or '') + '</span>'
                             if fundamentals and fundamentals.get('damod_industry') else '')
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">① 퀄리티 필터 — ROIC vs WACC' + _ind_tag1 + '</div>'
                    '<div class="analysis-metric-row">'
                    '<div class="analysis-chip"><div class="chip-label">ROIC</div>'
                    '<div class="chip-value">' + str(round(roic_val, 1)) + '%</div></div>'
                    '<div class="analysis-chip"><div class="chip-label">WACC (재레버링)</div>'
                    '<div class="chip-value">' + str(round(wacc_val, 1)) + '%</div></div>'
                    '<div class="analysis-chip"><div class="chip-label">스프레드</div>'
                    '<div class="chip-value ' + sp_cls + '">' + sp_sign + str(round(spread, 1)) + '%p</div></div>'
                    + ind_c +
                    '</div>'
                    '<div class="analysis-verdict ' + v_cls + '">' + v_txt + '</div>'
                    '<details class="analysis-hint-details"><summary>▶ 해석 보기</summary>'
                    '<div class="analysis-hint">' + hint + '</div></details>'
                    '</div>',
                    unsafe_allow_html=True
                )
            else:
                w = '⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다'
                if fundamentals and fundamentals.get('error'):
                    w = '⚠️ ' + str(fundamentals['error'])
                st.markdown('<div class="analysis-card"><div class="analysis-card-title">① 퀄리티 필터 — ROIC vs WACC</div>'
                            '<div class="analysis-verdict verdict-wait">' + w + '</div></div>', unsafe_allow_html=True)
        else:
            render_premium_lock('📊', '퀄리티 필터 — ROIC vs WACC 분석',
                '투하자본이익률(ROIC)과 가중평균자본비용(WACC)을 비교해 실질적 가치 창출 기업을 선별합니다.')

        # ② EV/EBITDA
        if PREMIUM_UNLOCKED:
            ev_eb = fundamentals.get('ev_ebitda')          if fundamentals else None
            ind_ev= fundamentals.get('industry_ev_ebitda') if fundamentals else None
            if ev_eb is not None:
                disc  = ((ind_ev - ev_eb) / ind_ev * 100) if ind_ev else None
                d_cls = 'positive' if (disc or 0) > 0 else 'negative'
                v_cls = ('verdict-buy' if (disc or 0) > 20 else
                         'verdict-watch' if (disc or 0) > 0 else
                         'verdict-pass') if disc is not None else 'verdict-watch'
                v_txt = ('✅ 업종 대비 ' + str(round(disc)) + '% 할인' if (disc or 0) > 0
                         else '⚠️ 업종 대비 ' + str(round(-(disc or 0))) + '% 프리미엄') if disc is not None else '업종 EV/EBITDA 매핑 불가'
                hint  = ('저평가 구간. 이익 성장 시 밸류에이션 정상화 기대.' if (disc or 0) > 0
                         else '미래 성장 프리미엄 반영. 성장 둔화 시 멀티플 압축 리스크.' if disc is not None
                         else '업종 중앙값 없음. 절대 배수(10~20x)와 직접 비교 권장.')
                i_c   = ('<div class="analysis-chip"><div class="chip-label">업종 중앙값</div><div class="chip-value">'
                         + str(round(ind_ev, 1)) + 'x</div></div>') if ind_ev else ''
                d_c   = ('<div class="analysis-chip"><div class="chip-label">할인율</div><div class="chip-value ' + d_cls + '">'
                         + ('+' if (disc or 0) > 0 else '') + str(round(disc or 0)) + '%</div></div>') if disc is not None else ''
                _ind_tag2 = (' <span style="font-size:0.72rem;font-weight:400;color:#a6adc8;background:#313244;'
                             'border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:middle;">🏷️ '
                             + (fundamentals.get('damod_industry') or '') + '</span>'
                             if fundamentals and fundamentals.get('damod_industry') else '')
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">② 밸류 필터 — EV/EBITDA 상대 배수' + _ind_tag2 + '</div>'
                    '<div class="analysis-metric-row">'
                    '<div class="analysis-chip"><div class="chip-label">EV/EBITDA</div>'
                    '<div class="chip-value">' + str(round(ev_eb, 1)) + 'x</div></div>'
                    + i_c + d_c +
                    '</div>'
                    '<div class="analysis-verdict ' + v_cls + '">' + v_txt + '</div>'
                    '<details class="analysis-hint-details"><summary>▶ 해석 보기</summary>'
                    '<div class="analysis-hint">' + hint + '</div></details>'
                    '</div>',
                    unsafe_allow_html=True
                )
            else:
                st.markdown('<div class="analysis-card"><div class="analysis-card-title">② 밸류 필터 — EV/EBITDA 상대 배수</div>'
                            '<div class="analysis-verdict verdict-wait">⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다</div></div>', unsafe_allow_html=True)
        else:
            render_premium_lock('💹', '밸류 필터 — EV/EBITDA 업종 대비 분석',
                '개별 종목 EV/EBITDA를 다모다란 업종 중앙값과 비교해 저평가 여부를 정량적으로 판단합니다.')

        # ③ DCF
        if PREMIUM_UNLOCKED:
            dcf_iv = fundamentals.get('dcf_value')    if fundamentals else None
            price  = fundamentals.get('current_price') if fundamentals else None
            wacc_u = fundamentals.get('wacc_used')    if fundamentals else None
            if dcf_iv is not None and dcf_iv > 0:
                margin = ((dcf_iv - price) / dcf_iv * 100) if price else None
                m_cls  = 'positive' if (margin or 0) > 0 else 'negative'
                v_cls  = ('verdict-buy' if (margin or 0) > 30 else
                          'verdict-watch' if (margin or 0) > 0 else 'verdict-pass') if margin is not None else 'verdict-wait'
                v_txt  = ('✅ 안전마진 ' + str(round(margin or 0)) + '% — 저평가 신호' if (margin or 0) > 30 else
                          '✅ 내재가치 대비 ' + str(round(margin or 0)) + '% 여유' if (margin or 0) > 0 else
                          '⚠️ 현재가 내재가치 ' + str(round(-(margin or 0))) + '% 초과') if margin is not None else '주가 데이터 확인 중'
                hint   = ('안전마진 30% 이상 — 가치투자 기준 충족. FCF 기반 보수적 추정치입니다.' if (margin or 0) > 30 else
                          '양의 안전마진이나 30% 미만. 성장 기대치와 종합 판단 권장.' if (margin or 0) > 0 else
                          '현재가가 FCF 내재가치 초과. 고성장 기업에서는 일반적. Reverse DCF 카드로 내재 성장률 확인 권장.')
                p_c = ('<div class="analysis-chip"><div class="chip-label">현재가</div><div class="chip-value">$' + str(round(price, 2)) + '</div></div>') if price else ''
                m_c = ('<div class="analysis-chip"><div class="chip-label">안전마진</div><div class="chip-value ' + m_cls + '">'
                       + ('+' if (margin or 0) > 0 else '') + str(round(margin or 0)) + '%</div></div>') if margin is not None else ''
                w_c = ('<div class="analysis-chip"><div class="chip-label">할인율(WACC)</div><div class="chip-value">' + str(round(wacc_u, 1)) + '%</div></div>') if wacc_u else ''
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">③ DCF 보조 검증 — 내재가치 안전마진</div>'
                    '<div class="analysis-metric-row">'
                    '<div class="analysis-chip"><div class="chip-label">DCF 내재가치</div><div class="chip-value">$' + str(round(dcf_iv, 2)) + '</div></div>'
                    + p_c + m_c + w_c +
                    '</div>'
                    '<div class="analysis-verdict ' + v_cls + '">' + v_txt + '</div>'
                    '<details class="analysis-hint-details"><summary>▶ 해석 보기</summary>'
                    '<div class="analysis-hint">' + hint + '</div></details>'
                    '</div>',
                    unsafe_allow_html=True
                )
            else:
                reason = ''
                if fundamentals:
                    if (fundamentals.get('ebit') or 0) <= 0:
                        reason = ' (영업손실 구간 — DCF 산출 불가)'
                    elif fundamentals.get('ebitda') is None:
                        reason = ' (DA 데이터 미확인)'
                st.markdown('<div class="analysis-card"><div class="analysis-card-title">③ DCF 보조 검증 — 내재가치 안전마진</div>'
                            '<div class="analysis-verdict verdict-wait">⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다' + reason + '</div></div>', unsafe_allow_html=True)
        else:
            render_premium_lock('🔬', 'DCF 보조 검증 — 내재가치 안전마진',
                'WACC 할인율을 적용한 DCF 모델로 내재가치를 산출하고 현재가 대비 안전마진(30% 이상 권장)을 확인합니다.')

        # ④ Reverse DCF
        if PREMIUM_UNLOCKED:
            rdcf_g = fundamentals.get('rdcf_implied_g') if fundamentals else None
            wacc_u = fundamentals.get('wacc_used')      if fundamentals else None
            fcf_v  = fundamentals.get('fcf')            if fundamentals else None
            if rdcf_g is not None:
                if rdcf_g < 0:
                    v_cls, v_txt = 'verdict-buy',   '✅ 성장 없어도 주가 정당 — 보수적 저평가 신호'
                    hint = '현재 EV 기준 FCF/EV 가 WACC 초과 → 제로 성장으로도 주가 정당화. 저평가 가능성.'
                elif rdcf_g <= 5:
                    v_cls, v_txt = 'verdict-watch', '📌 저성장(' + str(rdcf_g) + '%/yr) 내재 — 적정 밸류에이션'
                    hint = '내재 성장률 ' + str(rdcf_g) + '% — GDP 성장률 수준. 성숙 기업에 합리적, 고성장 기업엔 저평가 가능.'
                elif rdcf_g <= 15:
                    v_cls, v_txt = 'verdict-watch', '📌 중성장(' + str(rdcf_g) + '%/yr) 내재 — 기대치 점검 필요'
                    hint = '내재 성장률 ' + str(rdcf_g) + '%. 과거 매출성장률과 비교해 달성 가능성 판단 권장.'
                elif rdcf_g <= 30:
                    v_cls, v_txt = 'verdict-pass',  '⚠️ 고성장(' + str(rdcf_g) + '%/yr) 내재 — 달성 여부가 핵심'
                    hint = '내재 성장률 ' + str(rdcf_g) + '%. 미달 시 멀티플 압축 리스크. 실제 YoY 매출성장률과 비교 권장.'
                else:
                    v_cls, v_txt = 'verdict-pass',  '🚨 초고성장(' + str(rdcf_g) + '%/yr) 내재 — 투기적 프리미엄'
                    hint = '내재 성장률 ' + str(rdcf_g) + '% — 비현실적 수준. 성장 기대 실망 시 급격한 주가 조정 위험.'
                f_c = ('<div class="analysis-chip"><div class="chip-label">FCF (연간)</div><div class="chip-value">$' + str(round(fcf_v/1e9, 1)) + 'B</div></div>') if fcf_v else ''
                w_c = ('<div class="analysis-chip"><div class="chip-label">WACC</div><div class="chip-value">' + str(round(wacc_u, 1)) + '%</div></div>') if wacc_u else ''
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">④ Reverse DCF — 주가 내재 성장률</div>'
                    '<div class="analysis-metric-row">'
                    '<div class="analysis-chip"><div class="chip-label">내재 성장률 (g)</div>'
                    '<div class="chip-value">' + str(rdcf_g) + '%/yr</div></div>'
                    + f_c + w_c +
                    '</div>'
                    '<div class="analysis-verdict ' + v_cls + '">' + v_txt + '</div>'
                    '<details class="analysis-hint-details"><summary>▶ 해석 보기</summary>'
                    '<div class="analysis-hint">' + hint + '</div></details>'
                    '</div>',
                    unsafe_allow_html=True
                )
            else:
                reason = ''
                if fundamentals:
                    if not fundamentals.get('fcf') or (fundamentals.get('fcf') or 0) <= 0:
                        reason = ' (FCF 음수 — 역DCF 산출 불가)'
                    elif not fundamentals.get('wacc_used'):
                        reason = ' (WACC 미확인)'
                st.markdown('<div class="analysis-card"><div class="analysis-card-title">④ Reverse DCF — 주가 내재 성장률</div>'
                            '<div class="analysis-verdict verdict-wait">⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다' + reason + '</div></div>', unsafe_allow_html=True)
        else:
            render_premium_lock('🔄', 'Reverse DCF — 주가 내재 성장률',
                '현재 주가에 시장이 요구하는 성장률을 역산합니다. 낮을수록 기대치 달성 부담이 적습니다.')

        # ⑤ PSR + Rule of 40
        if PREMIUM_UNLOCKED:
            psr_v  = fundamentals.get('psr')                if fundamentals else None
            r40_v  = fundamentals.get('rule_of_40')         if fundamentals else None
            rev_g  = fundamentals.get('revenue_growth_yoy') if fundamentals else None
            fcf_mg = fundamentals.get('fcf_margin')         if fundamentals else None
            is_hg  = fundamentals.get('is_high_growth', False) if fundamentals else False

            if psr_v is not None or r40_v is not None:
                hg_label = '🚀 고성장' if is_hg else '📊 일반'
                p_cls = 'positive' if (psr_v or 99) < 5 else ('negative' if (psr_v or 0) > 15 else '')
                psr_c = ('<div class="analysis-chip"><div class="chip-label">PSR (EV/Rev)</div>'
                         '<div class="chip-value ' + p_cls + '">' + str(round(psr_v, 1)) + 'x</div></div>') if psr_v is not None else ''
                rg_cls = 'positive' if (rev_g or 0) > 20 else ('negative' if (rev_g or 0) < 0 else '')
                rg_c  = ('<div class="analysis-chip"><div class="chip-label">매출성장(YoY)</div>'
                         '<div class="chip-value ' + rg_cls + '">' + ('+' if (rev_g or 0) > 0 else '') + str(round(rev_g or 0, 1)) + '%</div></div>') if rev_g is not None else ''
                fm_cls= 'positive' if (fcf_mg or 0) > 10 else ('negative' if (fcf_mg or 0) < 0 else '')
                fm_c  = ('<div class="analysis-chip"><div class="chip-label">FCF 마진</div>'
                         '<div class="chip-value ' + fm_cls + '">' + ('+' if (fcf_mg or 0) > 0 else '') + str(round(fcf_mg or 0, 1)) + '%</div></div>') if fcf_mg is not None else ''
                r40_c = r40_bar = v_cls2 = v_txt2 = hint2 = ''
                if r40_v is not None:
                    pct   = int(min(max(r40_v, 0), 80) / 80 * 100)
                    b_cls = 'pass' if r40_v >= 40 else ('watch' if r40_v >= 20 else 'fail')
                    v_cls2= 'verdict-buy' if r40_v >= 40 else ('verdict-watch' if r40_v >= 20 else 'verdict-pass')
                    v_txt2= ('✅ Rule of 40 충족 — 성장·수익성 균형 달성' if r40_v >= 40 else
                             '⚠️ Rule of 40 미충족 — 성장 또는 수익성 보강 필요' if r40_v >= 20 else
                             '🚨 Rule of 40 크게 미달 — 손익 구조 점검 필요')
                    rs    = str(round(rev_g, 1)) + '%' if rev_g is not None else '?'
                    fs    = str(round(fcf_mg, 1)) + '%' if fcf_mg is not None else '?'
                    hint2 = ('매출성장(' + rs + ') + FCF마진(' + fs + ') = ' + str(round(r40_v, 1)) + '. '
                             + ('40이상 — 건강한 고성장 기업.' if r40_v >= 40 else '40 미만 — 성장·수익성 중 하나 보강 필요.'))
                    r40_c = ('<div class="analysis-chip"><div class="chip-label">Rule of 40</div>'
                             '<div class="chip-value">' + str(round(r40_v, 1)) + '</div></div>')
                    r40_bar = '<div class="r40-bar-wrap"><div class="r40-bar-fill ' + b_cls + '" style="width:' + str(pct) + '%"></div></div>'
                psr_hint = ''
                if psr_v is not None:
                    psr_hint = ('PSR ' + str(round(psr_v, 1)) + 'x: '
                                + ('저PSR — 매출 대비 합리적.' if psr_v < 5
                                   else '중PSR — 성장 프리미엄.' if psr_v < 15
                                   else '고PSR — 강한 성장 기대 필수.'))
                full_hint = ' '.join(filter(None, [psr_hint, hint2]))
                st.markdown(
                    '<div class="analysis-card">'
                    '<div class="analysis-card-title">⑤ 고성장 지표 — PSR + Rule of 40 '
                    '<span style="font-weight:400;color:#6c7086">(' + hg_label + ')</span></div>'
                    '<div class="analysis-metric-row">'
                    + psr_c + rg_c + fm_c + r40_c +
                    '</div>'
                    + r40_bar
                    + ('<div class="analysis-verdict ' + v_cls2 + '">' + v_txt2 + '</div>' if v_txt2 else '')
                    + ('<details class="analysis-hint-details"><summary>▶ 해석 보기</summary>'
                       '<div class="analysis-hint">' + full_hint + '</div></details>' if full_hint else '')
                    + '</div>',
                    unsafe_allow_html=True
                )
            else:
                st.markdown('<div class="analysis-card"><div class="analysis-card-title">⑤ 고성장 지표 — PSR + Rule of 40</div>'
                            '<div class="analysis-verdict verdict-wait">⏳ EDGAR 재무 데이터 연동 후 자동 계산됩니다</div></div>', unsafe_allow_html=True)
        else:
            render_premium_lock('🚀', '고성장 지표 — PSR + Rule of 40',
                '매출 성장률과 FCF 마진의 합(Rule of 40)으로 고성장 기업의 건전성을 평가합니다.')

    # 디버그 expander (with 블록 밖)
    if PREMIUM_UNLOCKED and fundamentals:
        dbg = fundamentals.get('debug', {})
        err = fundamentals.get('error', '')
        with st.expander('🔍 EDGAR 데이터 연동 상태', expanded=False):
            if err:
                st.error('오류: ' + str(err))
            cik    = dbg.get('cik', '—')
            loaded = dbg.get('facts_loaded', False)
            is_etf = dbg.get('is_etf', False)
            tags   = dbg.get('tags_found', {})
            cols   = st.columns(3)
            cols[0].metric('CIK', cik or '미발견')
            cols[1].metric('EDGAR 로드', '✅' if loaded else '❌')
            cols[2].metric('ETF 여부', 'ETF' if is_etf else '일반주')
            damod_ind = fundamentals.get('damod_industry', '')
            wacc_u    = fundamentals.get('wacc_used')
            ind_src   = fundamentals.get('industry_source', '')
            src_info  = (damod_ind or 'None') + ' (' + ind_src + ')'
            wacc_info = str(round(wacc_u, 2)) + '%' if wacc_u else '—'
            st.caption('업종 매핑: ' + src_info + ' | WACC: ' + wacc_info)
            if tags:
                ok  = [t for t, v in tags.items() if v]
                nok = [t for t, v in tags.items() if not v]
                if ok:
                    st.success('확인된 태그: ' + ', '.join(ok))
                if nok:
                    st.warning('미확인 태그: ' + ', '.join(nok))



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
    render_stock_header(ticker_sym, fin_data)

    # ── 프리미엄 분석 섹션 ──────────────────────────────────────
    # 기사 유무와 무관하게 항상 표시 (EDGAR + Damodaran 연동, 24시간 캐시)
    if PREMIUM_UNLOCKED:
        _price  = float(fin_data.get('current') or fin_data.get('prev_close') or 0)
        _mcap   = float(fin_data.get('mcap') or 0)
        # 업종 오버라이드: 사용자가 선택한 값이 있으면 캐시 키에 반영
        _ind_ov = st.session_state.get('ind_override_' + ticker_sym, '')
        fundamentals = fetch_premium_fundamentals(ticker_sym, _price, _mcap, _ind_ov)
    else:
        fundamentals = None
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


# ── 사이드바 ─────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ 종목 관리")
    st.caption(f"업데이트: {kst_now_str()}")

    if st.button("🔄 새로고침", use_container_width=True):
        clear_cache()
        st.rerun()

    st.divider()

    # ── 종목 추가 (티커 자동 조회) ───────────────────────────────
    st.subheader("➕ 종목 추가")

    # session_state 초기화
    if 'pending_ticker' not in st.session_state:
        st.session_state['pending_ticker'] = ''
    if 'pending_name' not in st.session_state:
        st.session_state['pending_name'] = ''

    with st.form("ticker_lookup_form", clear_on_submit=False):
        ticker_input = st.text_input(
            "티커 입력 (예: AAPL, SOXL)",
            max_chars=10,
            value=st.session_state['pending_ticker']
        )
        lookup_clicked = st.form_submit_button("🔍 회사명 조회", use_container_width=True)

    if lookup_clicked:
        t = ticker_input.upper().strip()
        if t:
            with st.spinner("조회 중..."):
                name = lookup_company_name(t)
            if name:
                st.session_state['pending_ticker'] = t
                st.session_state['pending_name'] = name
            else:
                st.warning("회사명을 찾을 수 없습니다. 티커를 확인해 주세요.")
                st.session_state['pending_name'] = ''
        else:
            st.warning("티커를 입력해 주세요.")

    if st.session_state['pending_name']:
        st.info(f"**{st.session_state['pending_ticker']}** → {st.session_state['pending_name']}")
        if st.button("➕ 종목 추가 확정", use_container_width=True, type="primary"):
            try:
                add_ticker(st.session_state['pending_ticker'], st.session_state['pending_name'])
                clear_cache()
                st.success(f"{st.session_state['pending_ticker']} 추가 완료!")
                st.session_state['pending_ticker'] = ''
                st.session_state['pending_name'] = ''
                st.rerun()
            except Exception as e:
                st.error(f"추가 실패: {e}")

    st.divider()

    # ── 종목 삭제 ────────────────────────────────────────────────
    st.subheader("🗑️ 종목 삭제")
    tickers_raw = load_tickers()
    if tickers_raw:
        ticker_options = {f"{t['ticker']} ({t['company_name']})": t['ticker'] for t in tickers_raw}
        selected_label = st.selectbox("삭제할 종목 선택", list(ticker_options.keys()))
        if st.button("삭제", use_container_width=True, type="secondary"):
            try:
                remove_ticker(ticker_options[selected_label])
                clear_cache()
                st.success(f"{ticker_options[selected_label]} 삭제 완료!")
                st.rerun()
            except Exception as e:
                st.error(f"삭제 실패: {e}")
    else:
        st.info("등록된 종목이 없습니다.")


# ── 메인 ─────────────────────────────────────────────────────────
st.title("📰 Frank News Dashboard")
st.caption("미국 주식 뉴스 자동 수집 대시보드 | Finnhub 기반 | 2시간마다 업데이트")
st.divider()

df = load_news()
tickers = load_tickers()

if df.empty or not tickers:
    st.info("📭 아직 수집된 뉴스가 없습니다. GitHub Actions가 2시간마다 뉴스를 수집합니다.")
    st.stop()

# 탭 레이블: 기empty and not tickers:
    st.info("📭 등록된 종목이 없거나 오늘 수집된 기사가 없습니다. 사이드바에서 종목을 추가하세요.")
else:
    ticker_list = tickers if tickers else []
    if not ticker_list:
        st.info("사이드바에서 종목을 추가하세요.")
    else:
        counts = {}
        if not df.empty and 'ticker' in df.columns:
            counts = df.groupby('ticker').size().to_dict()

        tab_labels = []
        for t in ticker_list:
            sym = t['ticker']
            n   = counts.get(sym, 0)
            tab_labels.append(f"{sym} ({n})" if n > 0 else sym)

        tabs = st.tabs(tab_labels)
        for tab, t in zip(tabs, ticker_list):
            sym = t['ticker']
            with tab:
                if not df.empty and 'ticker' in df.columns:
                    ticker_df = df[df['ticker'] == sym].copy()
                else:
                    ticker_df = pd.DataFrame()
                render_ticker_content(sym, ticker_df)
