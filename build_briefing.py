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

sys.path.insert(0, os.path.dirname(__file__))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SNAP_PATH = 'data/krx_snapshot_latest.json'   # 한국 숫자 (Action 생성)
US_PATH = 'data/us_issues.json'               # 미국 이슈 분석 (마켓 브리프 세션 생성)
KR_PATH = 'data/kr_issues.json'               # 한국 이슈 분석 (마켓 브리프 세션 생성)
STANCE_PATH = 'data/stance.json'              # 오늘의 스탠스 A/B/C (마켓 브리프 세션 생성)
TRIGGERS_PATH = 'data/triggers.json'          # 트리거 조건·상태 (마켓 브리프 세션 생성)
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


def build_generators(snap, us, kr, stance, triggers):
    """key -> 교체할 내부 HTML(문자열) 또는 None(건드리지 않음)."""
    g = {}
    dd = snap.get('bas_dd', '')
    idx = snap.get('index', {})
    flow = snap.get('investor_flow', {})
    flow_ok = isinstance(flow, dict) and flow.get('status') == 'ok'

    # 날짜 라벨(여러 헤더에 동일 key로 반복 사용)
    if dd:
        g['asof'] = date_label(dd)

    # (지수 카드/매수구간 범위는 편집성 분석과 섞여 있어 자동 주입 대상에서 제외 —
    #  마켓 브리프 세션이 산문과 함께 관리)
    k = idx.get('KOSPI') or {}

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
    return g


def render(html, snap, us, kr, stance, triggers):
    gens = build_generators(snap, us, kr, stance, triggers)
    filled, skipped, missing = [], [], []
    for key, content in gens.items():
        if content is None:
            skipped.append(key)
            continue
        # 마커 접두사는 KRX(한국 숫자)·US(미국 이슈)·KR(한국 이슈·스탠스) 모두 허용 — 같은 교체 로직
        pat = re.compile(
            r'(<!--(?:KRX|US|KR)-START:' + re.escape(key) + r'-->)(?:.*?)(<!--(?:KRX|US|KR)-END:'
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
    if not any((snap, us, kr, stance, triggers)):
        print('주입할 소스가 하나도 없음 — 종료')
        return
    with open(HTML_PATH, encoding='utf-8') as f:
        html = f.read()
    out = render(html, snap, us, kr, stance, triggers)
    if out != html:
        with open(HTML_PATH, 'w', encoding='utf-8') as f:
            f.write(out)
        print(f'저장 완료: {HTML_PATH}')
    else:
        print('변경 없음.')


if __name__ == '__main__':
    main()
