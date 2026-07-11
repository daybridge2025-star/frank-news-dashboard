"""
브리핑 HTML 조립기 — 여러 소스를 각자 담당 구간에만 주입한다.

소스별 소유자와 마커 접두사 (자세한 설명은 BRIEFING_PIPELINE.md 참고):
  data/krx_snapshot_latest.json  <!--KRX-START/END:key-->  Action(숫자, 하루 1회 자동)
  data/us_issues.json            <!--US-START/END:key-->   마켓 브리프 세션(미국 이슈 분석)
  data/kr_issues.json,           <!--KR-START/END:key-->   마켓 브리프 세션(한국 이슈 분석 ·
  data/stance.json,                                         오늘의 스탠스 A/B/C ·
  data/triggers.json                                        트리거 발동/임박 판단 — 셋 다 KR 접두사)

원칙:
- 자리표시(마커)가 있는 구간만 건드린다 — 마커 밖의 손으로 쓴 분석 산문은 절대 손대지 않는다.
- 데이터가 없으면(status != ok, 값 None) 해당 구간을 건드리지 않고 기존 "미확보" 표기를 그대로 둔다.
  즉 숫자를 지어내지도, 있던 걸 지우지도 않는다.
- 마커를 못 찾으면 경고만 남기고 계속(마켓 브리프 세션이 마커를 지웠을 가능성 대비).

단일 스냅샷은 '전일(해당 영업일)' 값만 제공하므로 투자자 수급의 연초·이번달 누적 컬럼은
채우지 않는다(시계열 누적이 필요 — 별도 작업).
"""

import sys
import os
import re
import json
import html as _html
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SNAP_PATH = 'data/krx_snapshot_latest.json'   # 한국 숫자 (Action 생성)
US_PATH = 'data/us_issues.json'               # 미국 이슈 분석 (마켓 브리프 세션 생성)
KR_PATH = 'data/kr_issues.json'               # 한국 이슈 분석 (마켓 브리프 세션 생성)
STANCE_PATH = 'data/stance.json'              # 오늘의 스탠스 A/B/C (마켓 브리프 세션 생성)
TRIGGERS_PATH = 'data/triggers.json'          # 트리거 조건·상태 (마켓 브리프 세션 생성)
US_SNAP_PATH = 'data/us_snapshot_latest.json'  # 미국·글로벌 시세 (Action 생성 — fetch_us_snapshot.py)
ECON_CAL_PATH = 'data/econ_calendar_latest.json'  # 이번 주 경제 캘린더 (Action 생성 — fetch_econ_calendar.py)
HTML_PATH = 'reports/macro-strategy-briefing.html'


def esc(s):
    return _html.escape(str(s), quote=False)

MINUS = '−'  # 기존 HTML이 쓰는 유니코드 마이너스(−)와 맞춤

# 업종별 등락률 표에서 제외할 '지수(사이즈/스타일)' 이름 — 진짜 업종만 남긴다
_NON_SECTOR = ('코스피', '코스닥', '200', '150', '100', '50', 'TOP',
               '대형주', '중형주', '소형주', '비중상한', '외국주포함')


# ── 포매터 ────────────────────────────────────────────────
def fmt_won(v):
    """원 단위 정수 → '+1,428억' / '−1.3조' (기존 표기 규칙)."""
    if v is None:
        return None
    sign = '+' if v >= 0 else MINUS
    a = abs(v)
    if a >= 1e12:
        return f'{sign}{a / 1e12:,.1f}조'
    return f'{sign}{a / 1e8:,.0f}억'


def fmt_pct(p):
    if p is None:
        return None
    sign = '+' if p >= 0 else MINUS
    return f'{sign}{abs(p):.2f}%'


def fmt_idx(v):
    if v is None:
        return None
    return f'{v:,.0f}'


def color_of(v):
    return 'var(--bull)' if (v or 0) >= 0 else 'var(--bear)'


def date_label(bas_dd):
    return f'{int(bas_dd[4:6])}/{int(bas_dd[6:8])}'


# ── 데이터 helper ─────────────────────────────────────────
def _net(iv, name):
    for r in iv or []:
        if r.get('투자자구분') == name:
            return r.get('순매수')
    return None


def _industry(sectors):
    out = []
    for s in sectors or []:
        nm = s.get('name') or ''
        if any(x in nm for x in _NON_SECTOR):
            continue
        if s.get('change_pct') is None:
            continue
        out.append(s)
    return out


# ── 구간 생성기 ───────────────────────────────────────────
def _fm_cell(value, maxabs):
    """투자자별 수급 '전일' 셀 하나(막대+값). value None이면 None(=건드리지 않음)."""
    if value is None:
        return None
    side = 'buy' if value >= 0 else 'sell'
    width = round(abs(value) / maxabs * 47) if maxabs else 0
    return (f'<div class="fm-cell"><div class="track"><div class="zero"></div>'
            f'<div class="fill {side}" style="width:{width}%"></div></div>'
            f'<span class="v {side}">{fmt_won(value)}</span></div>')


def _flow_cells(flow, market, period_field='investor_value', suffix=''):
    """market의 외국인/개인/기관 셀 3개(전일/이번달/연초 공용, period_field로 선택)."""
    iv = (flow.get(market) or {}).get(period_field)
    if not iv:
        return {}
    vals = {
        'foreign': _net(iv, '외국인'),
        'individual': _net(iv, '개인'),
        'institution': _net(iv, '기관합계'),
    }
    present = [abs(v) for v in vals.values() if v is not None]
    maxabs = max(present) if present else 0
    prefix = 'flow_' + ('kospi' if market == 'KOSPI' else 'kosdaq') + '_'
    return {prefix + k + suffix: _fm_cell(v, maxabs) for k, v in vals.items()}


def _sector_rows(sectors, n=5):
    ind = _industry(sectors)
    if not ind:
        return None
    ind.sort(key=lambda s: s['change_pct'])  # 낙폭 큰 순
    rows = ''
    for s in ind[:n]:
        rows += (f'<tr><td>{s["name"]}</td>'
                 f'<td class="num" style="color:{color_of(s["change_pct"])}">'
                 f'{fmt_pct(s["change_pct"])}</td></tr>')
    return rows


def _foreign_top_rows(flow, market, n=5):
    ft = (flow.get(market) or {}).get('foreign_net_top')
    if not ft:
        return None
    buy, sell = ft.get('buy', []), ft.get('sell', [])
    rows = ''
    for i in range(n):
        b = buy[i] if i < len(buy) else None
        s = sell[i] if i < len(sell) else None
        bn = b['종목'] if b else '미확보'
        ba = (f'<span style="color:var(--bull)">{fmt_won(b["순매수"])}</span>'
              if b else '<span style="color:var(--ink-3)">—</span>')
        sn = s['종목'] if s else '미확보'
        sa = (f'<span style="color:var(--bear)">{fmt_won(s["순매수"])}</span>'
              if s else '<span style="color:var(--ink-3)">—</span>')
        rows += (f'<tr><td>{i + 1}</td><td>{bn}</td><td class="num">{ba}</td>'
                 f'<td>{sn}</td><td class="num">{sa}</td></tr>')
    return rows


def _pension_side_rows(flow, side, n=5):
    """side: 'buy'(순매수 상위) 또는 'sell'(순매도 상위). KOSPI+KOSDAQ 합산 상위 n개.
    get_net_purchase_top()이 이미 매수·매도 양쪽을 다 반환하므로 추가 수집 없이
    렌더링만 하면 된다 — buy는 순매수 큰 순, sell은 순매도(가장 음수) 큰 순."""
    combined = []
    for market, kr in (('KOSPI', '코스피'), ('KOSDAQ', '코스닥')):
        pt = (flow.get(market) or {}).get('pension_net_top')
        if pt:
            for x in pt.get(side, []):
                combined.append((x['종목'], kr, x['순매수']))
    if not combined:
        return None
    combined.sort(key=lambda t: t[2], reverse=(side == 'buy'))
    color = 'var(--bull)' if side == 'buy' else 'var(--bear)'
    rows = ''
    for i, (nm, kr, net) in enumerate(combined[:n], 1):
        rows += (f'<tr><td>{i}</td><td>{nm}</td><td>{kr}</td>'
                 f'<td class="num" style="color:{color}">{fmt_won(net)}</td></tr>')
    return rows


def _pension_rows(flow, n=5):
    return _pension_side_rows(flow, 'buy', n)


def _pension_sell_rows(flow, n=5):
    return _pension_side_rows(flow, 'sell', n)


_CAL_FLAG = {'US': '🇺🇸', 'KR': '🇰🇷'}
_KST = timezone(timedelta(hours=9))


def _cal_value(v, unit):
    """Finnhub actual/estimate/prev 값 포맷 — 없으면 '—', 있으면 unit(%,K 등)을 붙인다."""
    if v is None or v == '':
        return '—'
    if isinstance(v, float):
        s = f'{v:,.0f}' if v.is_integer() else f'{v:,.1f}'
    else:
        s = str(v)
    unit = (unit or '').strip()
    if unit == '%':
        return f'{s}%'
    return f'{s}{unit}' if unit else s


def _econ_calendar_rows(cal):
    """data/econ_calendar_latest.json → 이번 주 경제 캘린더 표 행. 이벤트 없으면 None(기존 유지).
    같은 날짜가 이어지면 날짜 셀은 첫 행에만 표시(연기금 상위 표와 달리 시계열 나열이라 그룹핑)."""
    events = (cal or {}).get('events') or []
    if not events:
        return None
    today = datetime.now(_KST).strftime('%Y-%m-%d')
    rows, last_date = [], None
    for ev in events:
        t = ev.get('time', '') or ''
        dpart, tpart = t[:10], t[11:16]
        show_date = dpart != last_date
        last_date = dpart
        try:
            mmdd = f'{int(dpart[5:7])}/{int(dpart[8:10])}'
        except (ValueError, IndexError):
            mmdd = dpart
        if not show_date:
            date_cell = ''
        elif dpart == today:
            date_cell = f'<b style="color:var(--accent)">{mmdd}</b>'
        else:
            date_cell = mmdd
        flag = _CAL_FLAG.get(ev.get('country', ''), '')
        unit = ev.get('unit')
        rows.append(
            f'<tr><td>{date_cell}</td><td>{esc(tpart)}</td><td>{flag}</td>'
            f'<td>{esc(ev.get("event", ""))}</td>'
            f'<td class="num">{_cal_value(ev.get("actual"), unit)}</td>'
            f'<td class="num">{_cal_value(ev.get("estimate"), unit)}</td>'
            f'<td class="num">{_cal_value(ev.get("prev"), unit)}</td></tr>')
    return ''.join(rows)


def _render_issue_cards(src):
    """{issues:[...]} 형태(us_issues.json / kr_issues.json 공용) → .iss 카드 HTML. 없으면 None(기존 유지)."""
    issues = (src or {}).get('issues') or []
    if not issues:
        return None
    cards = []
    for it in issues:
        title = esc(it.get('title', ''))
        ds = ''.join(f'<div class="ds">{esc(d)}</div>' for d in it.get('desc', []))
        imps = ''
        for imp in it.get('impacts', []):
            lvl = imp.get('level', 'w')
            if lvl not in ('g', 'w', 'c', 's'):
                lvl = 'w'
            imps += (f'<div class="imp {lvl}"><span class="bg">{esc(imp.get("strategy", ""))}</span>'
                     f'<span>{esc(imp.get("text", ""))}</span></div>')
        cards.append(f'<div class="iss"><div class="hd">{title}</div>{ds}{imps}</div>')
    return '\n  ' + '\n  '.join(cards) + '\n  '


def _render_stance(stance):
    """stance.json({strategies:[{label,headline,detail}]}) → .srow 블록 HTML. 없으면 None(기존 유지)."""
    rows = (stance or {}).get('strategies') or []
    if not rows:
        return None
    out = []
    for r in rows:
        out.append(
            f'<div class="srow"><span class="bg">{esc(r.get("label", ""))}</span>\n'
            f'      <div><div class="sv">{esc(r.get("headline", ""))}</div>\n'
            f'      <p>{esc(r.get("detail", ""))}</p></div>\n    </div>')
    return '\n    ' + '\n    '.join(out) + '\n    '


def _mkt_generators(usm):
    """data/us_snapshot_latest.json → 핵심 지표 카드의 '값' 마커(MKT 접두사).
    수집 실패 항목은 키 자체를 만들지 않아 렌더러가 건너뛴다(기존 표기 유지)."""
    g = {}
    y = (usm or {}).get('yahoo') or {}
    f = (usm or {}).get('fred') or {}

    def price(k):
        return (y.get(k) or {}).get('price')

    p = price('vix')
    if p is not None:
        g['mkt_vix_v'] = f'{p:.1f}'
    kr = y.get('usdkrw') or {}
    if kr.get('price') is not None:
        g['mkt_usdkrw_v'] = f'{kr["price"]:,.1f}'
        if kr.get('prev') is not None:
            dchg = kr['price'] - kr['prev']
            g['mkt_usdkrw_chg'] = ('+' if dchg >= 0 else MINUS) + f'{abs(dchg):,.1f}원'
    p = price('wti')
    if p is not None:
        g['mkt_wti_v'] = f'${p:,.1f}'
    p = price('gold')
    if p is not None:
        g['mkt_gold_v'] = f'${p:,.0f}'
    p = price('copper')
    if p is not None:
        g['mkt_copper_v'] = f'${p:,.2f}'

    t10 = (f.get('t10y') or {}).get('value')
    t2 = (f.get('t2y') or {}).get('value')
    if t10 is not None:
        g['mkt_t10y_v'] = f'{t10:.2f}'
    if t2 is not None:
        g['mkt_t2y_v'] = f'{t2:.2f}'
    if t10 is not None and t2 is not None:
        bp = round((t10 - t2) * 100)
        g['mkt_spread_v'] = ('+' if bp >= 0 else MINUS) + str(abs(bp))
    hy = (f.get('hy_oas') or {}).get('value')
    if hy is not None:
        g['mkt_hy_v'] = str(round(hy * 100))
    fl = (f.get('fed_lower') or {}).get('value')
    fu = (f.get('fed_upper') or {}).get('value')
    if fl is not None and fu is not None:
        g['mkt_fed_v'] = f'{fl:.2f}–{fu:.2f}'

    for key, label in (('sp500', 'S&P 500'), ('nasdaq', '나스닥'), ('dow', '다우')):
        item = y.get(key)
        if not item or item.get('price') is None:
            continue
        g[f'mkt_{key}_v'] = f'{item["price"]:,.0f}'
        if item.get('change_pct') is not None:
            g[f'mkt_{key}_chg'] = fmt_pct(item['change_pct'])
        if item.get('ytd_start') is not None and item.get('ytd_high') is not None:
            g[f'mkt_{key}_range'] = _render_range_bar(
                item['ytd_start'], item['price'], item['ytd_high'], label)
    return g


def _render_range_bar(start, now, high, label):
    """KOSPI 위치 바(.range)와 동일한 컴포넌트 — 연초/현재/고점 3점을 트랙에 표시.
    KOSPI 버전과 달리 '매수구간'(.zone) 개념이 없는 일반 지수용이라 그 부분만 뺐다."""
    lo, hi = min(start, now, high), max(start, now, high)
    span = (hi - lo) or 1  # 셋이 모두 같은 극단값(무변동)인 방어
    def pos(x):
        return (x - lo) / span * 100
    def lbl_pos(p):
        return max(3, min(94, p))  # 라벨이 카드 밖으로 안 밀리게 3~94%로 여유
    p_start, p_now, p_high = pos(start), pos(now), pos(high)
    return (
        f'<div class="range" aria-label="{esc(label)} 연초 {start:,.0f}, 고점 {high:,.0f}, 현재 {now:,.0f}">'
        f'<div class="track"></div>'
        f'<div class="mk" style="left:{p_start:.1f}%"></div>'
        f'<div class="mk now" style="left:{p_now:.1f}%"></div>'
        f'<div class="mk" style="left:{p_high:.1f}%"></div>'
        f'<div class="lb top" style="left:{lbl_pos(p_start):.1f}%">연초 <b>{start:,.0f}</b></div>'
        f'<div class="lb top" style="left:{lbl_pos(p_now):.1f}%">현재 <b>{now:,.0f}</b></div>'
        f'<div class="lb top" style="left:{lbl_pos(p_high):.1f}%">고점 <b>{high:,.0f}</b></div>'
        f'</div>')


_STATUS_ORDER = {'hit': 0, 'approaching': 1, 'dormant': 2}
_STATUS_BADGE = {
    'hit':         ('발동', 'var(--crit)'),
    'approaching': ('임박', 'var(--warn)'),
}


def _render_triggers(triggers):
    """triggers.json({triggers:[{category,tag,cond,act,status,strategies}]}) → .trg 카드 HTML.
    상태 우선순위(발동>임박>평시)로 정렬하고, 평시가 아니면 tag에 배지를 붙인다.
    카드 좌측 색상바(category: buy/sell/watch)는 액션 종류를 뜻하며 status와는 별개다.
    strategies(예: ["A","B"])는 .bg와 같은 모양의 배지로 cond 앞에 표시 — 단 .bg 클래스 자체는
    CSS에서 .stance .srow .bg / .iss .imp .bg 로만 스코프돼 있어 .trg 안에선 안 먹으므로,
    <style> 블록을 건드리지 않고 인라인 스타일로 같은 모양을 재현한다."""
    rows = (triggers or {}).get('triggers') or []
    if not rows:
        return None
    ordered = sorted(rows, key=lambda t: _STATUS_ORDER.get(t.get('status'), 2))
    cards = []
    for t in ordered:
        cat = t.get('category', 'watch')
        if cat not in ('buy', 'sell', 'watch'):
            cat = 'watch'
        tag = esc(t.get('tag', ''))
        badge = _STATUS_BADGE.get(t.get('status'))
        if badge:
            label, color = badge
            tag += f' <span style="color:{color}; font-weight:800;">· {label}</span>'
        badge_style = ('display:inline-block; font-size:10px; font-weight:800; letter-spacing:.04em; '
                       'border-radius:6px; padding:1px 6px; margin-right:4px; '
                       'border:1px solid var(--ring); color:var(--ink);')
        strat_badges = ''.join(
            f'<span style="{badge_style}">{esc(s)}</span> ' for s in t.get('strategies', []))
        cards.append(
            f'<div class="trg {cat}"><div class="st"></div><div>\n'
            f'    <div class="tag">{tag}</div>\n'
            f'    <div class="cond">{strat_badges}{esc(t.get("cond", ""))}</div>\n'
            f'    <div class="act">{esc(t.get("act", ""))}</div></div></div>')
    return '\n\n  ' + '\n\n  '.join(cards) + '\n\n  '


def build_generators(snap, us, kr, stance, triggers, usm, cal):
    """key -> 교체할 내부 HTML(문자열) 또는 None(건드리지 않음)."""
    g = {}
    dd = snap.get('bas_dd', '')
    idx = snap.get('index', {})
    flow = snap.get('investor_flow', {})
    flow_ok = isinstance(flow, dict) and flow.get('status') == 'ok'

    # 날짜 라벨(여러 헤더에 동일 key로 반복 사용)
    if dd:
        g['asof'] = date_label(dd)

    # 헤더 날짜 라인 — 수기 갱신이 누락되며 화석화되던 라벨을 자동화.
    # 렌더 시각(시계)이 아니라 스냅샷 수집 시각(fetched_at) 기준이라 언제 다시 렌더해도 같은 결과.
    m_fa = re.match(r'(\d{4})-(\d{2})-(\d{2}) (\d{2}):\d{2}', snap.get('fetched_at') or '')
    if m_fa:
        y, mo, d_ = int(m_fa.group(1)), int(m_fa.group(2)), int(m_fa.group(3))
        wd = '월화수목금토일'[datetime(y, mo, d_).weekday()]
        g['hdr_date'] = f'{y}년 {mo}월 {d_}일 ({wd})'
        upd = f'최종 업데이트 {m_fa.group(2)}-{m_fa.group(3)} {m_fa.group(4)}시'
        if us and us.get('asof'):
            upd += f' · {esc(us["asof"])}'
        g['hdr_updated'] = upd

    # 지수 카드 — 숫자(종가·등락률·기준일)만 자동, 해석 꼬리는 편집 소유(마커 밖)
    k = idx.get('KOSPI') or {}
    for mkey, mdata in (('kospi', idx.get('KOSPI')), ('kosdaq', idx.get('KOSDAQ'))):
        if mdata and mdata.get('close') is not None:
            close = mdata['close']
            # 기존 카드 표기 관례: 코스피 '7,247'(정수), 코스닥 '785.0'(소수 1자리)
            g[f'idx_{mkey}_v'] = f'{close:,.0f}' if close >= 1000 else f'{close:,.1f}'
            if mdata.get('change_pct') is not None and dd:
                g[f'idx_{mkey}_chg'] = f'{fmt_pct(mdata["change_pct"])} ({date_label(dd)})'

    # 업종별 등락률(지수 데이터는 로그인 불필요 — 키만 있으면 채워짐)
    g['sectors_kospi'] = _sector_rows((snap.get('sectors') or {}).get('KOSPI'))
    g['sectors_kosdaq'] = _sector_rows((snap.get('sectors') or {}).get('KOSDAQ'))

    # 투자자 수급 전일·이번달·연초 + 상위 종목(로그인 필요 — flow_ok일 때만)
    if flow_ok:
        for market in ('KOSPI', 'KOSDAQ'):
            g.update(_flow_cells(flow, market))
            g.update(_flow_cells(flow, market, 'investor_value_mtd', '_mtd'))
            g.update(_flow_cells(flow, market, 'investor_value_ytd', '_ytd'))
        g['foreign_top_kospi'] = _foreign_top_rows(flow, 'KOSPI')
        g['foreign_top_kosdaq'] = _foreign_top_rows(flow, 'KOSDAQ')
        g['pension_top'] = _pension_rows(flow)
        g['pension_sell_top'] = _pension_sell_rows(flow)
        # 기간 라벨(코스피·코스닥 공용 — 같은 기준일/월초/연초를 쓰므로 마커 1쌍만 필요)
        mtd_from, ytd_from = flow.get('mtd_from'), flow.get('ytd_from')
        if mtd_from and dd:
            g['mtd_label'] = f'{date_label(mtd_from)}~{date_label(dd)}'
        if ytd_from and dd:
            g['ytd_label'] = f'{date_label(ytd_from)}~{date_label(dd)}'

    # 연동 상태 노트
    sources = []
    if k.get('close') is not None:
        sources.append('지수·업종·종목 시세(Open API)')
    if flow_ok:
        sources.append('투자자별 수급·순매수 상위(정보데이터시스템)')
    if sources:
        cum_note = ('연초·이번달 누적은 KRX 서버가 해당 기간을 직접 합산해 조회한 값이다.'
                    if flow_ok else '연초·이번달 누적은 투자자 수급 로그인 연동 전까지 미확보로 남는다.')
        g['integration_note'] = (
            f'✅ KRX 직접 연동 가동 중 — {" · ".join(sources)}를 매 영업일 자동 수집·주입. '
            f'기준일 {date_label(dd)}. {cum_note} '
            f'확보 못 한 값은 지어내지 않고 "미확보/집계중"으로 남긴다.')

    # ── 미국 이슈(별도 소스 data/us_issues.json — 마켓 브리프 세션이 갱신) ──
    g['us_issues'] = _render_issue_cards(us)
    if us and us.get('asof'):
        g['us_issues_asof'] = esc(us['asof'])

    # ── 한국 이슈 분석(data/kr_issues.json — 마켓 브리프 세션이 갱신) ──
    # 날짜는 별도 필드 없이 위 KRX 'asof' 마커를 그대로 재사용(같은 기준일 데이터를 해석하므로)
    g['kr_issues'] = _render_issue_cards(kr)

    # ── 오늘의 스탠스 A/B/C(data/stance.json — 마켓 브리프 세션이 갱신) ──
    g['stance'] = _render_stance(stance)

    # ── 트리거 발동/임박 판단(data/triggers.json — 마켓 브리프 세션이 갱신) ──
    g['triggers'] = _render_triggers(triggers)

    # ── 미국·글로벌 시세(data/us_snapshot_latest.json — Action 자동) ──
    g.update(_mkt_generators(usm))

    # ── 이번 주 경제 캘린더(data/econ_calendar_latest.json — Action 자동, Finnhub) ──
    g['econ_calendar'] = _econ_calendar_rows(cal)
    if cal and cal.get('week_start') and cal.get('week_end'):
        ws, we = cal['week_start'], cal['week_end']
        g['econ_cal_week'] = f'{int(ws[5:7])}/{int(ws[8:10])}~{int(we[5:7])}/{int(we[8:10])}'
    return g


def render(html, snap, us, kr, stance, triggers, usm, cal):
    gens = build_generators(snap, us, kr, stance, triggers, usm, cal)
    filled, skipped, missing = [], [], []
    for key, content in gens.items():
        if content is None:
            skipped.append(key)
            continue
        # 마커 접두사: KRX(한국 숫자)·MKT(미국 시세)·US(미국 이슈)·KR(한국 이슈·스탠스) — 같은 교체 로직
        pat = re.compile(
            r'(<!--(?:KRX|US|KR|MKT)-START:' + re.escape(key) + r'-->)(?:.*?)(<!--(?:KRX|US|KR|MKT)-END:'
            + re.escape(key) + r'-->)', re.DOTALL)
        html, n = pat.subn(lambda m: m.group(1) + content + m.group(2), html)
        if n == 0:
            missing.append(key)
        else:
            filled.append(f'{key}×{n}')
    print('채움:', ', '.join(filled) or '(없음)')
    if skipped:
        print('건너뜀(데이터 없음, 기존 유지):', ', '.join(skipped))
    if missing:
        print('경고 — 마커 없음(HTML에서 자리표시를 못 찾음):', ', '.join(missing))
    return html


def _warn_if_editorial_stale(us, snap):
    """미국 이슈의 asof 날짜가 KRX 기준일과 어긋나면 경고(로그만, 강제 없음).
    보통 두 날짜는 같은 m/d다(미국 전일 세션과 한국 직전 영업일이 같은 달력 날짜) —
    다르면 대개 us_issues.json 갱신 누락이며, 드물게 한쪽 휴장으로 정상 어긋남일 수 있다."""
    asof = (us or {}).get('asof') or ''
    dd = (snap or {}).get('bas_dd') or ''
    m = re.search(r'(\d{1,2})\s*/\s*(\d{1,2})', asof)
    if m and len(dd) == 8:
        if (int(m.group(1)), int(m.group(2))) != (int(dd[4:6]), int(dd[6:8])):
            print(f'⚠️ 신선도 경고: 미국 이슈 asof "{asof}" ≠ KRX 기준일 '
                  f'{int(dd[4:6])}/{int(dd[6:8])} — us_issues.json 갱신 누락 가능성 '
                  f'(한쪽 휴장이면 정상). 마켓 브리프 세션 확인 필요.')


def _load(path, label):
    if not os.path.exists(path):
        print(f'{label} 없음: {path} — 해당 영역 주입 생략')
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'{label} 로드 실패({path}): {e} — 해당 영역 주입 생략')
        return {}


def main():
    if not os.path.exists(HTML_PATH):
        print(f'HTML 없음: {HTML_PATH} — 주입 생략')
        return
    snap = _load(SNAP_PATH, '한국 스냅샷')
    us = _load(US_PATH, '미국 이슈')
    kr = _load(KR_PATH, '한국 이슈')
    stance = _load(STANCE_PATH, '오늘의 스탠스')
    triggers = _load(TRIGGERS_PATH, '트리거')
    usm = _load(US_SNAP_PATH, '미국 시세')
    cal = _load(ECON_CAL_PATH, '경제 캘린더')
    if not any((snap, us, kr, stance, triggers, usm, cal)):
        print('주입할 소스가 하나도 없음 — 종료')
        return
    _warn_if_editorial_stale(us, snap)
    with open(HTML_PATH, encoding='utf-8') as f:
        html = f.read()
    out = render(html, snap, us, kr, stance, triggers, usm, cal)
    if out != html:
        with open(HTML_PATH, 'w', encoding='utf-8') as f:
            f.write(out)
        print(f'저장 완료: {HTML_PATH}')
    else:
        print('변경 없음.')


if __name__ == '__main__':
    main()
