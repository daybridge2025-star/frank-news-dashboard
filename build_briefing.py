"""
브리핑 HTML 조립기 — 여러 소스를 각자 담당 구간에만 주입한다.

소스별 소유자와 마커 접두사 (자세한 설명은 BRIEFING_PIPELINE.md 참고):
  data/krx_snapshot_latest.json  <!--KRX-START/END:key-->  Action(숫자, 하루 1회 자동)
  data/us_issues.json            <!--US-START/END:key-->   마켓 브리프 세션(미국 이슈 분석)
  data/kr_issues.json,           <!--KR-START/END:key-->   마켓 브리프 세션(한국 이슈 분석 ·
  data/stance.json,                                         오늘의 스탠스 A/B/C · 오늘의 한 줄(lead) ·
  data/triggers.json                                        트리거 발동/임박 판단 — 셋 다 KR 접두사)
  data/earnings_calendar.json    <!--MKT-START/END:sched_box-->  마켓 브리프 세션(전략 관련 기업 실적 일정)

독자 노출 문장의 문체·형식(해요체, JM님 호칭, 이모지 없음, 기사형 카드)은 utils/article.py가 코드로 고정한다 —
편집 소스(JSON)는 `**굵게**`·`==형광펜==` 두 가지 가벼운 마크업만 쓰고, 그 밖의 HTML은 전부 이스케이프된다.

원칙:
- 자리표시(마커)가 있는 구간만 건드린다 — 마커 밖의 손으로 쓴 분석 산문은 절대 손대지 않는다.
- 숫자를 지어내지 않는다. 최신 스냅샷에 값이 없으면 마지막으로 확보된 값을 쓰되, **그 값의 실제 기준일을
  라벨에 그대로 표기**한다(과거 값을 새 날짜 라벨 아래 두지 않는다 — 투자자 수급 `flow_asof`/`flow_notice`,
  KOSPI 위치 바의 연초·고점 이월 참고). 이월할 값조차 없으면 해당 구간은 건드리지 않는다.
- 변하는 숫자(현재가·거리·비중·트리거 상태)는 손글에 쓰지 않고 여기서 자동 계산해 넣는다
  (`kospi_readout`, 지표 카드 눈금 `mkt_*_tick`).
- 마커를 못 찾으면 경고만 남기고 계속(마켓 브리프 세션이 마커를 지웠을 가능성 대비).
"""

import sys
import os
import re
import glob
import json
import html as _html
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from utils import article  # noqa: E402  (표준 라이브러리만 씀 — Action 러너에서도 그대로 import 가능)

SNAP_PATH = 'data/krx_snapshot_latest.json'   # 한국 숫자 (Action 생성)
US_PATH = 'data/us_issues.json'               # 미국 이슈 분석 (마켓 브리프 세션 생성)
KR_PATH = 'data/kr_issues.json'               # 한국 이슈 분석 (마켓 브리프 세션 생성)
STANCE_PATH = 'data/stance.json'              # 오늘의 스탠스 A/B/C (마켓 브리프 세션 생성)
TRIGGERS_PATH = 'data/triggers.json'          # 트리거 조건·상태 (마켓 브리프 세션 생성)
US_SNAP_PATH = 'data/us_snapshot_latest.json'  # 미국·글로벌 시세 (Action 생성 — fetch_us_snapshot.py)
ECON_CAL_PATH = 'data/econ_calendar_latest.json'  # 이번 주 경제 캘린더 (Action 생성 — fetch_econ_calendar.py)
FOMC_PATH = 'data/fomc_schedule.json'         # 연준 공식 FOMC 일정 (수동, 연 1회)
EARN_PATH = 'data/earnings_calendar.json'     # 전략 관련 기업 실적 발표 일정 (마켓 브리프 세션이 갱신)
SBX_PATH = 'data/strategy_b_screen.json'  # 전략 B 편입 스크리너 (Action 생성 — monitor_strategy_b.py)
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


def _snap_files():
    """날짜별 KRX 스냅샷 파일(오름차순). 파일명이 krx_snapshot_YYYYMMDD.json이라 이름순=날짜순."""
    return sorted(glob.glob(os.path.join('data', 'krx_snapshot_2*.json')))


def _last_good_flow():
    """최신 스냅샷에 투자자 수급이 없을 때(KRX 응답 비어있음 등) 마지막으로 status=ok였던 스냅샷의
    (bas_dd, investor_flow)를 돌려준다. 없으면 (None, None).
    값은 그대로 보여주되 라벨은 그 값의 실제 기준일을 써야 한다 — 과거 값을 새 날짜 라벨 아래 두지 않는다."""
    for f in reversed(_snap_files()):
        try:
            with open(f, encoding='utf-8') as fh:
                s = json.load(fh)
        except Exception:
            continue
        fl = s.get('investor_flow')
        if isinstance(fl, dict) and fl.get('status') == 'ok' and fl.get('KOSPI'):
            return s.get('bas_dd'), fl
    return None, None


def _ytd_carry(market='KOSPI'):
    """지수 연초값·연중고점이 최신 스냅샷에 없을 때(YTD 시계열 수집 실패) 가장 최근에 확보된 값을 이월한다.
    ytd_start는 1/2 종가라 불변, ytd_high는 신고가가 나오지 않는 한 불변(호출부에서 당일 고가와 max)."""
    for f in reversed(_snap_files()):
        try:
            with open(f, encoding='utf-8') as fh:
                s = json.load(fh)
        except Exception:
            continue
        k = (s.get('index') or {}).get(market) or {}
        if k.get('ytd_start') is not None and k.get('ytd_high') is not None:
            return k['ytd_start'], k['ytd_high']
    return None, None


def _provisional(kr, bas_dd):
    """kr_issues.json의 provisional(한국장 마감 직후 언론 집계 종가) 중 스냅샷 기준일(bas_dd)보다 *최신*인 것만 돌려준다.
    KRX 스냅샷은 한국장 마감 뒤 늦은 저녁에야 들어와, 편집 세션(UTC 07·21시대)은 그날 종가를 언론 집계로만 안다.
    이때 자동 구간(지수 카드·위치 바·판독·칩)이 어제 확정치를 '현재'로 보여주면 손글·트리거와 모순된다(2026-10-15 실측:
    카드는 6,009 '손절선 +0.15% 위', 트리거는 5,554 'Stop-Loss 발동').
    스냅샷이 따라잡으면(bas_dd >= asof) 저절로 무시된다 — 세션이 지우지 않아도 확정치로 돌아간다.
    반환: {'dd': 'YYYYMMDD', 'source': str, 'KOSPI': {'close', 'change_pct'}, 'KOSDAQ': {...}} 또는 None(형식 오류 포함)."""
    p = (kr or {}).get('provisional')
    if not isinstance(p, dict):
        return None
    dd = re.sub(r'\D', '', str(p.get('asof') or ''))
    if len(dd) != 8 or not (bas_dd and len(str(bas_dd)) == 8) or dd <= str(bas_dd):
        return None
    out = {'dd': dd, 'source': str(p.get('source') or '언론 집계')}
    for key, mname in (('kospi', 'KOSPI'), ('kosdaq', 'KOSDAQ')):
        m = p.get(key)
        if not isinstance(m, dict):
            continue
        c, ch = m.get('close'), m.get('change_pct')
        if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0:
            out[mname] = {'close': float(c),
                          'change_pct': ch if isinstance(ch, (int, float)) and not isinstance(ch, bool) else None}
    return out if ('KOSPI' in out or 'KOSDAQ' in out) else None


def _ro(txt):
    """'억'→'으로', '조'→'로' 처럼 마지막 글자의 받침으로 조사(으)로를 고른다(ㄹ 받침은 '로')."""
    last = str(txt)[-1:]
    if '가' <= last <= '힣':
        return '로' if (ord(last) - 0xAC00) % 28 in (0, 8) else '으로'
    return '로'


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


def _flow_summary(flow, market, when='전일'):
    """수급 매트릭스 하단 자동 해설 <p>들. 스냅샷에서 매번 재계산되므로 화석화되지 않는다.
    전일 3주체 방향+매물 흡수 구조 → 외국인 최대 순매수/순매도 종목(로테이션 힌트) →
    이번달 외국인 추세. 깊은 편집성 해석(로테이션의 의미 등)은 kr_issues.json 몫으로 남긴다.
    when: 첫 줄의 기준 표기 — 최신 수급이 없어 과거 확정치를 쓸 땐 '전일' 대신 그 날짜('10/7')를 넣는다."""
    mk = flow.get(market) or {}
    iv = mk.get('investor_value')
    if not iv:
        return None
    f, ind, inst = _net(iv, '외국인'), _net(iv, '개인'), _net(iv, '기관합계')
    pen = _net(iv, '연기금 등')
    if f is None or ind is None or inst is None:
        return None
    trio = [('외국인', f), ('개인', ind), ('기관', inst)]
    seg = ' · '.join(f'{n} {fmt_won(v)}' for n, v in trio)
    sellers = [n for n, v in trio if v < 0]
    top_buyer = max(trio, key=lambda x: x[1])
    line1 = f'<b>{when} 수급</b> {seg}.'
    if top_buyer[1] > 0 and sellers and top_buyer[0] not in sellers:
        line1 += f' {top_buyer[0]}이 {"·".join(sellers)} 매물을 받아냈어요.'
        # 연기금은 기관의 하위 항목 — 방향이 기관 전체와 갈릴 때만 별도 언급(오해 방지)
        if pen is not None and top_buyer[0] == '기관' and pen < 0:
            line1 += f' 다만 기관 안의 연기금은 {fmt_won(pen)}{_ro(fmt_won(pen))} 순매도 쪽이에요.'
    ps = [line1]

    ft = mk.get('foreign_net_top') or {}
    b = (ft.get('buy') or [None])[0]
    s = (ft.get('sell') or [None])[0]
    if b and s:
        ps.append(f'<b>외국인 순매수 1위</b> {esc(b["종목"])}({fmt_won(b["순매수"])}) '
                  f'· <b>순매도 1위</b> {esc(s["종목"])}({fmt_won(s["순매수"])}). '
                  f'종목 사이 로테이션이 어느 쪽으로 가는지 보여줘요.')

    ivm = mk.get('investor_value_mtd')
    if ivm:
        fm = _net(ivm, '외국인')
        im = _net(ivm, '개인')
        if fm is not None and im is not None:
            fdir = '순매도' if fm < 0 else '순매수'
            idir = '순매수' if im >= 0 else '순매도'
            # 최신 수급이 없어 과거 확정치를 쓸 땐 '이번달'이 아니라 실제 집계 구간을 밝힌다
            mtd_txt = '이번달 누적'
            if when != '전일' and flow.get('mtd_from'):
                mtd_txt = f'{date_label(flow["mtd_from"])}~{when} 누적'
            tail = ''
            if fm < 0 <= im:
                tail = ' 외국인이 판 물량을 개인이 받아내는 흐름이에요.'
            elif im < 0 <= fm:
                tail = ' 외국인이 사고 개인이 파는 흐름이에요.'
            ps.append(f'<b>{mtd_txt}</b> 외국인은 {fmt_won(fm)} {fdir}, 개인은 {fmt_won(im)} {idir}예요.{tail}')

    return ''.join(f'<p class="fsum">{p}</p>' for p in ps)


def _sector_cols(snap):
    """업종표의 선택 컬럼 표시 여부 — 연초 대비(ytd_pct)·대표종목(leaders)이 하나라도 수집됐을 때만 보인다.
    pykrx 확장 수집이 실패하면 두 칸이 전 행 '—'로 비어 표가 지저분해지므로 컬럼 자체를 숨긴다."""
    has_ytd = has_lead = False
    for mk in ('KOSPI', 'KOSDAQ'):
        for s in _industry((snap.get('sectors') or {}).get(mk)):
            has_ytd = has_ytd or s.get('ytd_pct') is not None
            has_lead = has_lead or bool(s.get('leaders'))
    return has_ytd, has_lead


def _sector_head(has_ytd, has_lead):
    """업종표 머리행(<tr>) — 두 시장 표가 같은 컬럼 구성을 쓰므로 마커 1개(sectors_head)를 두 번 쓴다."""
    th = '<th>업종</th><th>당일</th>'
    if has_ytd:
        th += '<th>연초 대비</th>'
    if has_lead:
        th += '<th>대표종목 (시총 상위 3 · 당일)</th>'
    return f'<tr>{th}</tr>'


def _sector_rows(sectors, has_ytd=True, has_lead=True):
    """전체 업종지수 표(당일 내림차순) — 당일 [| 연초 대비] [| 대표종목(시총 상위, 당일 등락)].
    선택 컬럼은 _sector_cols가 판정한 경우에만 그린다. 켜진 컬럼 안의 개별 결측은 '—'."""
    ind = _industry(sectors)
    if not ind:
        return None
    ind.sort(key=lambda s: s['change_pct'], reverse=True)  # 당일 상승 순
    rows = ''
    for s in ind:
        cells = (f'<td>{s["name"]}</td>'
                 f'<td class="num" style="color:{color_of(s["change_pct"])}">'
                 f'{fmt_pct(s["change_pct"])}</td>')
        if has_ytd:
            ytd = s.get('ytd_pct')
            cells += ('<td class="num">' +
                      (f'<span style="color:{color_of(ytd)}">{fmt_pct(ytd)}</span>'
                       if ytd is not None else '—') + '</td>')
        if has_lead:
            leaders = s.get('leaders') or []
            if leaders:
                parts = []
                for l in leaders:
                    nm = esc(l.get('name', ''))
                    c = l.get('chg_pct')
                    if c is not None:
                        parts.append(f'{nm} <span style="color:{color_of(c)}">{fmt_pct(c)}</span>')
                    else:
                        parts.append(nm)
                leader_cell = ' · '.join(parts)
            else:
                leader_cell = '—'
            cells += f'<td style="font-size:12px">{leader_cell}</td>'
        rows += f'<tr>{cells}</tr>'
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


_KST = timezone(timedelta(hours=9))


_SBX_SIGNAL_COLOR = {
    '편입 자격': 'var(--good)', '관찰': 'var(--warn)',
    '이탈 후보': 'var(--crit)', '데이터 부족': 'var(--ink-3)',
}


def _pct0(v, suffix='%'):
    """소수 없는 부호부 퍼센트. None이면 '—'."""
    if v is None:
        return '<span style="color:var(--ink-3)">—</span>'
    s = '+' if v >= 0 else MINUS
    return f'{s}{abs(v) * 100:.0f}{suffix}'


def _screen_rows(screen):
    """data/strategy_b_screen.json → 전략 B 스크리너 표 행. 없으면 None(기존 유지)."""
    rows = (screen or {}).get('rows') or []
    if not rows:
        return None
    out = ''
    for r in rows:
        mom, rs, above, gap = (r.get('mom_12_1'), r.get('rs_vs_bench'),
                               r.get('above_50d'), r.get('gap_50d'))
        sig = r.get('signal', '데이터 부족')
        color = _SBX_SIGNAL_COLOR.get(sig, 'var(--ink-3)')
        mom_cell = (f'<span style="color:{color_of(mom)}">{_pct0(mom)}</span>'
                    if mom is not None else '<span style="color:var(--ink-3)">—</span>')
        rs_cell = (f'<span style="color:{color_of(rs)}">{_pct0(rs, "%p")}</span>'
                   if rs is not None else '<span style="color:var(--ink-3)">—</span>')
        if above is None:
            ma_cell = '<span style="color:var(--ink-3)">—</span>'
        else:
            mark = '✅' if above else '❌'
            gap_txt = (f' <span style="color:{color_of(gap)}">{_pct0(gap, "%")[0:1]}'
                       f'{abs(gap) * 100:.1f}%</span>') if gap is not None else ''
            ma_cell = f'{mark}{gap_txt}'
        out += (f'<tr><td>{esc(r.get("name", ""))}'
                f'<br><span style="font-size:10px;color:var(--ink-3)">{esc(r.get("theme", ""))}</span></td>'
                f'<td class="num">{mom_cell}</td>'
                f'<td class="num">{rs_cell}</td>'
                f'<td class="num">{ma_cell}</td>'
                f'<td><span style="font-size:11px;font-weight:800;color:{color}">{sig}</span></td></tr>')
    return out


def _render_issue_cards(src, tag, meta=None):
    """{issues:[...]} 형태(us_issues.json / kr_issues.json 공용) → 기사형 카드(.art) HTML. 없으면 None(기존 유지).
    tag: 카드 위 알약 라벨('#미국장 마감'), meta: 제목 아래 기준 문구.
    v2({title, lead, sections[], impacts[], sources})와 구형 v1({title, desc[], impacts[]}) 모두 그린다."""
    issues = (src or {}).get('issues') or []
    if not issues:
        return None
    return '\n  ' + '\n  '.join(article.issue_card(it, tag, meta) for it in issues) + '\n  '


# 공포탐욕지수 등급(CNN) → (한글 라벨, 칩 색). 극단 구간은 경고색, 중립은 녹색.
_FNG_KO = {'extreme fear': ('극단적 공포', 'c'), 'fear': ('공포', 'w'), 'neutral': ('중립', 'g'),
           'greed': ('탐욕', 'w'), 'extreme greed': ('극단적 탐욕', 'c')}

# 핵심 지표 카드의 1년 레인지 바 (저점, 고점) — 카드의 "1년 저점/고점" 라벨과 같은 값
_RANGE_1Y = {'wti': (55.0, 117.6), 'gold': (3283.0, 5597.0), 'copper': (4.33, 6.72)}


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
    # 1년 레인지 눈금(.mrange) 위치 — 손글 % 값이 며칠만에 화석화돼 자동화. 저점·고점은 카드 라벨의 고정값
    # (신고가·신저가가 나오면 라벨과 함께 아래 상수를 갱신; 그 전까지는 0~100%로 클램프).
    for key, (lo, hi) in _RANGE_1Y.items():
        p = price(key)
        if p is not None and hi > lo:
            g[f'mkt_{key}_tick'] = f'<i style="left:{max(0.0, min(100.0, (p - lo) / (hi - lo) * 100)):.0f}%"></i>'

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
    # 지표별 기준일 배지(.asof) — 카드 제목 옆 'M/D' 표기. 수집 소스의 asof/date를 그대로 쓴다.
    def md(iso):
        try:
            return f'{int(iso[5:7])}/{int(iso[8:10])}'
        except Exception:
            return None
    for key in ('vix', 'usdkrw', 'wti', 'gold', 'copper', 'sp500', 'nasdaq', 'dow'):
        d = md((y.get(key) or {}).get('asof') or '')
        if d:
            g[f'mkt_{key}_d'] = d
    fd = {k: (f.get(k) or {}).get('date') or '' for k in ('t10y', 't2y', 'hy_oas', 'fed_upper')}
    for key, src in (('t10y', 't10y'), ('t2y', 't2y'), ('hy', 'hy_oas'), ('fed', 'fed_upper')):
        d = md(fd[src])
        if d:
            g[f'mkt_{key}_d'] = d
    sp = min((fd['t10y'], fd['t2y']) or ('',)) if fd['t10y'] and fd['t2y'] else ''
    if md(sp):
        g['mkt_spread_d'] = md(sp)

    # 공포탐욕지수(CNN) — 값·기준일·전일/1주/1개월 비교·상태 칩. 수집 실패 시 키가 없어 마지막 값과 그 기준일이 그대로 남는다.
    fg = (usm or {}).get('fear_greed') or {}
    sc = fg.get('score')
    if isinstance(sc, (int, float)):
        g['mkt_fng_v'] = f'{sc:.0f}'
        if md(fg.get('asof') or ''):
            g['mkt_fng_d'] = md(fg['asof'])
        rating = str(fg.get('rating') or '').lower()
        if rating not in _FNG_KO:      # 등급 문자열을 못 읽으면 CNN 구간표로 직접 판정
            rating = ('extreme fear' if sc < 25 else 'fear' if sc < 45 else 'neutral' if sc < 55
                      else 'greed' if sc < 75 else 'extreme greed')
        label, cls = _FNG_KO[rating]
        g['mkt_fng_chip'] = f'<span class="chip {cls}">{label}</span>'
        cmp_ = [f'{lab} {fg[k]:.0f}' for k, lab in (('prev_close', '전일'), ('prev_week', '1주 전'),
                                                    ('prev_month', '1개월 전'))
                if isinstance(fg.get(k), (int, float))]
        g['mkt_fng_sub'] = ' · '.join(cmp_ + ['CNN' if fg.get('source') == 'CNN' else 'CNN 미러'])

    # S&P 500 선행 PER(FactSet 주간 보고서) — 10년 평균 대비 위치를 칩으로 자동 판정
    va = (usm or {}).get('valuation') or {}
    pe = va.get('forward_pe')
    if isinstance(pe, (int, float)):
        g['mkt_pe_v'] = f'{pe:.1f}'
        if md(va.get('asof') or ''):
            g['mkt_pe_d'] = md(va['asof'])
        a5, a10 = va.get('avg5'), va.get('avg10')
        sub = 'Forward P/E'
        if isinstance(a5, (int, float)):
            sub += f' · 5년 평균 {a5:.1f}'
        if isinstance(a10, (int, float)):
            sub += f' · 10년 평균 {a10:.1f}'
        g['mkt_pe_sub'] = sub + ' · FactSet'
        if isinstance(a10, (int, float)) and a10 > 0:
            dev = pe / a10 - 1
            txt = ('10년 평균 수준' if abs(dev) < 0.015
                   else f'10년 평균 대비 {"+" if dev > 0 else MINUS}{abs(dev) * 100:.0f}%')
            g['mkt_pe_chip'] = f'<span class="chip {"g" if dev <= 0.03 else "w" if dev <= 0.12 else "c"}">{txt}</span>'
    return g


def _render_range_bar(start, now, high, label, zone=None):
    """위치 바(.range) 공통 컴포넌트 — 연초/현재/고점 3점을 트랙에 표시.
    zone=(lo, hi)를 주면 KOSPI처럼 '매수구간' 하이라이트(.zone)와 하단 라벨을 함께 그린다
    (구간 값 자체는 편집 판단이라 stance.json의 kospi_buy_zone에서 온다)."""
    lo, hi = min(start, now, high), max(start, now, high)
    span = (hi - lo) or 1  # 셋이 모두 같은 극단값(무변동)인 방어
    def pos(x):
        return (x - lo) / span * 100
    def lbl_pos(p):
        return max(3, min(94, p))  # 라벨이 카드 밖으로 안 밀리게 3~94%로 여유
    p_start, p_now, p_high = pos(start), pos(now), pos(high)
    aria = f'{label} 연초 {start:,.0f}, 고점 {high:,.0f}, 현재 {now:,.0f}'
    zone_html = ''
    zone_lbl = ''
    if zone and zone[0] is not None and zone[1] is not None:
        z_lo, z_hi = sorted((float(zone[0]), float(zone[1])))
        p_zlo = max(0.0, min(100.0, pos(z_lo)))
        p_zhi = max(0.0, min(100.0, pos(z_hi)))
        if p_zhi > p_zlo:
            zone_html = f'<div class="zone" style="left:{p_zlo:.1f}%; width:{p_zhi - p_zlo:.1f}%"></div>'
            zone_lbl = (f'<div class="lb bot" style="left:{lbl_pos((p_zlo + p_zhi) / 2):.1f}%">'
                        f'매수구간 {z_lo:,.0f}–{z_hi:,.0f}</div>')
            aria += f'. 분할매수 구간 {z_lo:,.0f}~{z_hi:,.0f}'
    return (
        f'<div class="range" aria-label="{esc(aria)}">'
        f'<div class="track"></div>'
        f'{zone_html}'
        f'<div class="mk" style="left:{p_start:.1f}%"></div>'
        f'<div class="mk now" style="left:{p_now:.1f}%"></div>'
        f'<div class="mk" style="left:{p_high:.1f}%"></div>'
        # '현재' 라벨은 연초·고점과 다른 행(.lb.now, CSS에서 top 오프셋 분리)에 둬서
        # 현재가가 고점/연초에 붙어도 텍스트가 겹치지 않는다(2026-07-23 재도입)
        f'<div class="lb top" style="left:{lbl_pos(p_start):.1f}%">연초 <b>{start:,.0f}</b></div>'
        f'<div class="lb top" style="left:{lbl_pos(p_high):.1f}%">고점 <b>{high:,.0f}</b></div>'
        f'<div class="lb now" style="left:{lbl_pos(p_now):.1f}%">현재 <b>{now:,.0f}</b></div>'
        f'{zone_lbl}'
        f'</div>')


# ── KOSPI 위치 판독 (손글 일지를 대체하는 자동 요약) ─────────────────────────
_TRG_KO = {'dormant': '대기', 'approaching': '임박', 'hit': '발동'}


def _trigger_status(triggers, tag):
    """triggers.json에서 tag가 정확히 일치하는 첫 트리거의 status(없으면 None)."""
    for t in (triggers or {}).get('triggers') or []:
        if (t.get('tag') or '').strip() == tag:
            return t.get('status')
    return None


def _trigger_chip(triggers, tag, hit_cls='c'):
    """지표 카드 칩 — 해당 트리거의 현재 status에서 자동 생성한다(손글 칩이 상태가 바뀐 뒤에도 남는 문제 방지)."""
    st = _trigger_status(triggers, tag)
    if not st:
        return None
    cls = {'hit': hit_cls, 'approaching': 'w', 'dormant': 'g'}.get(st, 'w')
    return f'<span class="chip {cls}">{tag} {_TRG_KO.get(st, st)}</span>'


def _kospi_chip(close, zone, stop):
    """KOSPI 카드 칩(상태 라벨) — 종가가 손절 참고선·매수구간 어디에 있는지로 자동 판정."""
    if stop is not None and close < stop:
        return 's', '손절 참고선 이탈'
    if stop is not None and close <= stop * 1.03:
        return 'c', '손절 참고선 근접'
    if zone:
        lo, hi = zone
        if lo <= close <= hi:
            return 'g', '매수구간 안'
        if close > hi:
            return 'w', '매수구간 위'
    return 'w', '매수구간 아래 · 대기'


def _kospi_readout(close, chg, dd, ys, yh, zone, stop, triggers, how='KRX 확정'):
    """위치 바 아래 자동 판독 <p> 3줄: 위치/거리/트리거. 숫자는 전부 스냅샷(또는 잠정 종가)·stance.json에서 계산한다.
    how: 값의 출처 라벨 — 'KRX 확정'(기본) 또는 잠정 종가일 때 '언론 집계 · KRX 확정 전'. 문장은 해요체."""
    ps = []
    head = f'<b>현재 {close:,.0f}</b> ({date_label(dd) + " " if dd else ""}{how}'
    if chg is not None:
        head += f', {fmt_pct(chg)}'
    head += ')'
    if ys is not None and yh is not None and yh > ys:
        pos = (close - ys) / (yh - ys) * 100
        head += (f'. 연초 {ys:,.0f}에서 고점 {yh:,.0f}까지의 {pos:.0f}% 지점이고, '
                 f'고점 대비 {_pct0(close / yh - 1)}예요')
    ps.append(head + '.')
    dist = []
    if zone:
        lo, hi = zone
        if close < lo:
            dist.append(f'매수구간({lo:,.0f}~{hi:,.0f}) 하단까지 +{(lo / close - 1) * 100:.1f}% 더 올라야 해요')
        elif close <= hi:
            dist.append(f'매수구간({lo:,.0f}~{hi:,.0f}) 안에 있어요')
        else:
            dist.append(f'매수구간({lo:,.0f}~{hi:,.0f})보다 +{(close / hi - 1) * 100:.1f}% 위에 있어요')
    if stop is not None:
        gap = (close / stop - 1) * 100
        dist.append(f'손절 참고선({stop:,.0f})보다 ' +
                    (f'+{gap:.2f}% 위예요' if gap >= 0 else f'{MINUS}{abs(gap):.2f}% 아래라 이탈 상태예요'))
    if dist:
        ps.append('. '.join(dist) + '.')
    st = [(tag, _trigger_status(triggers, tag)) for tag in ('Add', 'Stop-Loss')]
    st = [f'{tag} {_TRG_KO.get(s, s)}' for tag, s in st if s]
    if st:
        ps.append('트리거는 ' + ' · '.join(st) + ' 상태예요. 근거는 트리거 탭에서 볼 수 있어요.')
    return ''.join(f'<p class="kro">{p}</p>' for p in ps)


def _cal_refs(cal, snap):
    """일정 박스의 '지난 일정 거르기' 기준일 (미국 현지 날짜, 한국 날짜).
    렌더 시각(시계)이 아니라 데이터 수집 시각에서 받아, 언제 다시 빌드해도 같은 결과가 나온다."""
    for raw in ((cal or {}).get('fetched_at'), (snap or {}).get('fetched_at')):
        if not raw:
            continue
        try:
            t = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        except ValueError:
            continue
        if t.tzinfo is None:                       # 스냅샷의 'YYYY-MM-DD HH:MM'은 한국시간
            t = t.replace(tzinfo=_KST)
        return t.astimezone(timezone.utc).date(), t.astimezone(_KST).date()
    return None, None


def build_generators(snap, us, kr, stance, triggers, usm, cal, screen, fomc=None, earnings=None):
    """key -> 교체할 내부 HTML(문자열) 또는 None(건드리지 않음)."""
    g = {}
    dd = snap.get('bas_dd', '')
    idx = snap.get('index', {})
    flow = snap.get('investor_flow', {})
    flow_ok = isinstance(flow, dict) and flow.get('status') == 'ok'

    # 업종표 제목의 기준일 — 그 시장의 업종 데이터가 이번 스냅샷에 실제로 있을 때만 갱신한다.
    # (데이터가 비면 표 내용은 이전 날짜 그대로 남는데 라벨만 새 날짜로 바뀌는 불일치를 막는다.)
    if dd:
        for mkt, key in (('KOSPI', 'asof_kospi'), ('KOSDAQ', 'asof_kosdaq')):
            if (snap.get('sectors') or {}).get(mkt):
                g[key] = date_label(dd)

    # 헤더 날짜 라인 — 수기 갱신이 누락되며 화석화되던 라벨을 자동화.
    # 렌더 시각(시계)이 아니라 스냅샷 수집 시각(fetched_at) 기준이라 언제 다시 렌더해도 같은 결과.
    m_fa = re.match(r'(\d{4})-(\d{2})-(\d{2}) (\d{2}):\d{2}', snap.get('fetched_at') or '')
    if m_fa:
        y, mo, d_ = int(m_fa.group(1)), int(m_fa.group(2)), int(m_fa.group(3))
        wd = '월화수목금토일'[datetime(y, mo, d_).weekday()]
        g['hdr_date'] = f'{y}년 {mo}월 {d_}일 ({wd})'
        g['hdr_updated'] = f'최종 업데이트 {mo}월 {d_}일 {m_fa.group(4)}시'

    # 지수 연초값·연중고점 — 최신 스냅샷에 없으면(YTD 시계열 수집 실패) 가장 최근에 확보된 값을 이월한다.
    # (이월 없이 두면 위치 바만 과거 종가에 멈춰 지수 카드와 어긋난다.)
    ytd = {}
    for mname in ('KOSPI', 'KOSDAQ'):
        m = idx.get(mname) or {}
        s_, h_ = m.get('ytd_start'), m.get('ytd_high')
        if (s_ is None or h_ is None) and m.get('close') is not None:
            s_, h_ = _ytd_carry(mname)
            if h_ is not None and m.get('high') is not None:
                h_ = max(h_, m['high'])
        ytd[mname] = (s_, h_)
    k = idx.get('KOSPI') or {}
    ys, yh = ytd['KOSPI']
    bz = (stance or {}).get('kospi_buy_zone')
    zone = tuple(bz) if isinstance(bz, (list, tuple)) and len(bz) == 2 else None
    stop = (stance or {}).get('stop_loss_ref')

    # 지수 현재값 — 스냅샷(KRX 확정)이 기본. 스냅샷보다 최신인 잠정 종가(언론 집계, kr_issues.json의 provisional)가
    # 있으면 그것을 우선하고 라벨에 "언론 집계"를 밝힌다(스냅샷이 따라잡으면 _provisional이 알아서 None).
    prov = _provisional(kr, dd)

    def eff(mname):
        """(종가, 등락률%, 기준일 YYYYMMDD, 잠정 여부) — 값이 없으면 (None, None, None, False)."""
        pv = (prov or {}).get(mname)
        if pv:
            return pv['close'], pv.get('change_pct'), prov['dd'], True
        m = idx.get(mname) or {}
        if m.get('close') is not None:
            return m['close'], m.get('change_pct'), dd, False
        return None, None, None, False

    # 지수 카드 — 숫자(종가·등락률·기준일)와 고점 대비는 전부 자동(손글 꼬리 없음)
    for mkey, mname in (('kospi', 'KOSPI'), ('kosdaq', 'KOSDAQ')):
        close, chg_pct, when, is_prov = eff(mname)
        if close is None:
            continue
        # 기존 카드 표기 관례: 코스피 '7,247'(정수), 코스닥 '785.0'(소수 1자리)
        g[f'idx_{mkey}_v'] = f'{close:,.0f}' if close >= 1000 else f'{close:,.1f}'
        if chg_pct is not None and when:
            chg = f'{fmt_pct(chg_pct)} ({date_label(when)}{" 언론 집계" if is_prov else ""})'
            m_yh = ytd[mname][1]
            if m_yh:
                chg += f' · 고점 대비 {_pct0(close / m_yh - 1)}'
            g[f'idx_{mkey}_chg'] = chg
    # 코스닥 칩 — 연초 대비 등락(손글 "카드 값은 KRX 확정 … 언론" 꼬리 대체)
    kq_close = eff('KOSDAQ')[0]
    kq_ys = ytd['KOSDAQ'][0]
    if kq_close is not None and kq_ys:
        kq_ytd = kq_close / kq_ys - 1
        g['idx_kosdaq_chip'] = (f'<span class="chip {"g" if kq_ytd >= 0 else "w"}">'
                                f'연초 대비 {_pct0(kq_ytd)}</span>')

    # KOSPI 위치 바·판독·칩 — 연초/고점/현재는 데이터, 매수구간·손절 참고선은 편집 값(stance.json)
    kc, kchg, kwhen, kprov = eff('KOSPI')
    if kc is not None:
        if ys is not None and yh is not None:
            g['kospi_range'] = _render_range_bar(ys, kc, yh, 'KOSPI', zone=zone)
        g['kospi_readout'] = _kospi_readout(kc, kchg, kwhen, ys, yh, zone, stop, triggers,
                                            '언론 집계 · KRX 확정 전' if kprov else 'KRX 확정')
        cls, txt = _kospi_chip(kc, zone, stop)
        g['idx_kospi_chip'] = f'<span class="chip {cls}">{txt}</span>'

    # 업종별 등락률(지수 데이터는 로그인 불필요) — 연초 대비·대표종목 컬럼은 수집된 경우에만 표시
    has_ytd, has_lead = _sector_cols(snap)
    g['sectors_head'] = _sector_head(has_ytd, has_lead)
    g['sectors_kospi'] = _sector_rows((snap.get('sectors') or {}).get('KOSPI'), has_ytd, has_lead)
    g['sectors_kosdaq'] = _sector_rows((snap.get('sectors') or {}).get('KOSDAQ'), has_ytd, has_lead)

    # 투자자 수급 전일·이번달·연초 + 상위 종목(로그인 필요). 최신 스냅샷에 없으면(KRX 응답 비어있음)
    # 마지막 확정 스냅샷 값을 보여주되 기준일 라벨을 그 값의 날짜로 바꾸고 안내문을 붙인다 —
    # 과거 값을 새 날짜 라벨 아래 두면(이전 동작) 독자가 오래된 수급을 최신으로 오인한다.
    if flow_ok:
        flow_src, flow_dd = flow, dd
    else:
        flow_dd, flow_src = _last_good_flow()
    if flow_src:
        for market in ('KOSPI', 'KOSDAQ'):
            g.update(_flow_cells(flow_src, market))
            g.update(_flow_cells(flow_src, market, 'investor_value_mtd', '_mtd'))
            g.update(_flow_cells(flow_src, market, 'investor_value_ytd', '_ytd'))
        g['foreign_top_kospi'] = _foreign_top_rows(flow_src, 'KOSPI')
        g['foreign_top_kosdaq'] = _foreign_top_rows(flow_src, 'KOSDAQ')
        when = '전일' if flow_ok else date_label(flow_dd)
        g['flow_summary_kospi'] = _flow_summary(flow_src, 'KOSPI', when)
        g['flow_summary_kosdaq'] = _flow_summary(flow_src, 'KOSDAQ', when)
        g['pension_top'] = _pension_rows(flow_src)
        g['pension_sell_top'] = _pension_sell_rows(flow_src)
        # 기간 라벨(코스피·코스닥 공용 — 같은 기준일/월초/연초를 쓰므로 마커 1쌍만 필요)
        mtd_from, ytd_from = flow_src.get('mtd_from'), flow_src.get('ytd_from')
        if mtd_from and flow_dd:
            g['mtd_label'] = f'{date_label(mtd_from)}~{date_label(flow_dd)}'
        if ytd_from and flow_dd:
            g['ytd_label'] = f'{date_label(ytd_from)}~{date_label(flow_dd)}'
        g['flow_asof'] = date_label(flow_dd)
    else:
        g['flow_asof'] = '미수집'
    g['flow_col'] = '직전 영업일' if flow_ok else '최근 확정일'
    if flow_ok:
        g['flow_notice'] = ''
    elif flow_src:
        g['flow_notice'] = (
            '<p class="fnote">'
            f'KRX 투자자별 수급 응답이 {date_label(flow_dd)} 이후 비어 있어서, 수급 표와 상위종목은 '
            f'마지막 확정일({date_label(flow_dd)}) 값을 보여줘요. 그 뒤 거래일의 수급은 '
            '위쪽 \'#한국장 마감\' 카드의 언론 집계를 참고해 주세요.</p>')
    else:
        g['flow_notice'] = (
            '<p class="fnote">KRX 투자자별 수급 확정치를 아직 확보하지 못했어요. 지어내지 않고 비워 둘게요.</p>')

    # ── 미국 이슈(별도 소스 data/us_issues.json — 마켓 브리프 세션이 갱신) ──
    # 카드 위 알약 '#미국장 마감', 제목 아래 기준 문구는 us_issues.json의 asof를 그대로 쓴다.
    us_asof = (us or {}).get('asof')
    g['us_issues'] = _render_issue_cards(us, '#미국장 마감', us_asof)

    # ── 한국 이슈 분석(data/kr_issues.json — 마켓 브리프 세션이 갱신) ──
    # 카드의 기준일 문구는 카드가 해석하는 날 — 잠정 종가가 있으면 그 날, 없으면 스냅샷 기준일.
    # (업종표 제목의 기준일은 KRX 'asof_kospi'·'asof_kosdaq' 마커가 따로 맡는다.)
    kr_dd = prov['dd'] if prov else dd
    kr_meta = None
    if kr_dd:
        kr_meta = f'{article.kdate(kr_dd)} 마감 기준' + (' · 언론 집계(KRX 확정 전)' if prov else '')
    g['kr_issues'] = _render_issue_cards(kr, '#한국장 마감', kr_meta)

    # ── 오늘의 한 줄 + 스탠스 A/B/C(data/stance.json — 마켓 브리프 세션이 갱신) ──
    # 'lead'는 인사 배너 아래 한 줄 요약, 'stance'는 대시보드 헤더용 통합 블록, 'stance_a/b/c'는 전략 탭 각 pane
    # 상단용 단일 카드(2026-07-22 재배치 — 전략 탭에서는 헤더 블록이 숨겨지고 이 pane-분산 버전이 대신 보인다).
    lead = ((stance or {}).get('lead') or '').strip()
    g['lead'] = article.rich(lead) if lead else None
    g['stance'] = article.stance_all(stance)
    g['stance_a'] = article.stance_single(stance, 'A')
    g['stance_b'] = article.stance_single(stance, 'B')
    g['stance_c'] = article.stance_single(stance, 'C')

    # ── 트리거 발동/임박 판단(data/triggers.json — 마켓 브리프 세션이 갱신) ──
    g['triggers'] = article.trigger_cards(triggers)

    # ── 미국·글로벌 시세(data/us_snapshot_latest.json — Action 자동) ──
    g.update(_mkt_generators(usm))
    g['mkt_wti_chip'] = _trigger_chip(triggers, 'De-risk')
    g['mkt_usdkrw_chip'] = _trigger_chip(triggers, 'FX Watch', hit_cls='w')

    # ── 앞으로 볼 일정 박스: 경제 지표·연준(econ_calendar_latest.json — Action 자동, FRED + fomc_schedule.json)
    #    + 실적 발표(earnings_calendar.json — 마켓 브리프 세션). 둘 다 비면 ''라서 박스가 통째로 숨겨진다 —
    #    낡은 일정을 남겨 두지 않는다(옛 경제 캘린더 표와 달리 '기존 유지'가 없다).
    ref_us, ref_kr = _cal_refs(cal, snap)
    g['sched_box'] = article.sched_box(cal, (fomc or {}).get('meetings'), earnings, ref_us, ref_kr)
    g['sbx_screen'] = _screen_rows(screen)
    if screen and screen.get('fetched_at'):
        g['sbx_asof'] = esc(screen['fetched_at'])
    return g


def render(html, snap, us, kr, stance, triggers, usm, cal, screen, fomc=None, earnings=None):
    gens = build_generators(snap, us, kr, stance, triggers, usm, cal, screen, fomc, earnings)
    filled, skipped, missing = [], [], []
    for key, content in gens.items():
        if content is None:
            skipped.append(key)
            continue
        # 마커 접두사: KRX(한국 숫자)·MKT(미국 시세)·US(미국 이슈)·KR(한국 이슈·스탠스) — 같은 교체 로직
        pat = re.compile(
            r'(<!--(?:KRX|US|KR|MKT|SBX|PF)-START:' + re.escape(key) + r'-->)(?:.*?)(<!--(?:KRX|US|KR|MKT|SBX|PF)-END:'
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
    m = re.search(r'(\d{1,2})\s*/\s*(\d{1,2})', asof) or re.search(r'(\d{1,2})\s*월\s*(\d{1,2})\s*일', asof)
    if m and len(dd) == 8:
        if (int(m.group(1)), int(m.group(2))) != (int(dd[4:6]), int(dd[6:8])):
            print(f'⚠️ 신선도 경고: 미국 이슈 asof "{asof}" ≠ KRX 기준일 '
                  f'{int(dd[4:6])}/{int(dd[6:8])} — us_issues.json 갱신 누락 가능성 '
                  f'(한쪽 휴장이면 정상). 마켓 브리프 세션 확인 필요.')


# ── 작성 규칙 점검 (경고만) ────────────────────────────────────────────────
# 3개월간 에디션마다 경과를 덧붙여 페이지가 443KB·일지 문단 60여 개로 부풀었던 사고(2026-10-15 감사)의 재발 방지 가드.
_JARGON = ('WebSearch', 'Action', 'fetch_', 'bas_dd', 'pykrx')
_HTML_SOFT_LIMIT = 240_000  # 바이트. 정리 직후 약 156KB


def _lint_sources(us, kr, stance, triggers, bas_dd=None, earnings=None):
    """편집 소스(JSON)가 작성 규칙(최신 카드 1개·덮어쓰기·분량·내부 용어 금지)과 문체 가이드(해요체·이모지 없음·
    한 문장 90자 안팎 — BRIEFING_PIPELINE.md "문체·형식 가이드")를 지켰는지 점검한다. 경고만 출력 — 강제하지 않는다."""
    warn = []

    pv = (kr or {}).get('provisional')
    if pv is not None and bas_dd and _provisional(kr, bas_dd) is None:
        warn.append(f'kr_issues.provisional(asof {pv.get("asof") if isinstance(pv, dict) else pv!r})은 무시된다 — '
                    f'스냅샷({bas_dd})이 이미 따라잡았거나 형식 오류(asof "YYYY-MM-DD"·kospi.close 숫자). '
                    '새 종가로 덮어쓰거나 지울 것')

    def jargon(where, text):
        hit = [j for j in _JARGON if j in text]
        if hit:
            warn.append(f'{where}: 독자 문장에 내부 용어 {hit}')

    def check(where, text):
        """독자에게 보이는 문장 1개: 내부 용어 + 문체(해요체·이모지·문장 길이)."""
        text = str(text or '')
        jargon(where, text)
        warn.extend(article.style_warnings(where, text))

    for name, src in (('us_issues', us), ('kr_issues', kr)):
        issues = (src or {}).get('issues') or []
        if len(issues) > 1:
            warn.append(f'{name}.json issues {len(issues)}개 — 최신 에디션 카드 1개만 둔다(이전 카드는 삭제)')
        if (src or {}).get('asof'):
            check(f'{name}.asof', src['asof'])
        for it in issues:
            title = it.get('title', '')
            check(f'{name} 제목', title)
            if len(article.plain(title)) > 60:
                warn.append(f'{name} 제목 {len(article.plain(title))}자 — 60자 안팎의 한 문장으로')
            if it.get('desc') and not it.get('sections'):
                warn.append(f'{name}: 구형 desc 형식 — v2(lead·sections[]·impacts[])로 쓸 것(BRIEFING_PIPELINE.md)')
            for d in it.get('desc', []):
                jargon(f'{name} desc', d)
                if len(d) > 330:
                    warn.append(f'{name} desc {len(d)}자 — 1~2문장으로 쪼갤 것: {d[:24]}…')
            if it.get('lead'):
                check(f'{name} lead', it['lead'])
            secs = it.get('sections') or []
            if len(secs) > 4:
                warn.append(f'{name} sections {len(secs)}개 — 4개 이내로(소제목은 질문/주장형 한 줄)')
            for i, sc in enumerate(secs, 1):
                check(f'{name} 섹션{i} 소제목', sc.get('heading', ''))
                for b in sc.get('body') or []:
                    check(f'{name} 섹션{i} 본문', b)
                for c in sc.get('checks') or []:
                    check(f'{name} 섹션{i} 확인할 것', c)
                nt = sc.get('note')
                for n in ([nt] if isinstance(nt, str) else (nt or [])):
                    check(f'{name} 섹션{i} 각주', n)
            for imp in it.get('impacts') or []:
                check(f'{name} 영향 {imp.get("strategy", "?")}', imp.get('text', ''))
            jargon(f'{name} 출처', it.get('sources', ''))

    if (stance or {}).get('lead'):
        check('stance lead', stance['lead'])
    elif stance:
        warn.append('stance.json에 lead(오늘의 한 줄) 없음 — 인사 배너 아래 한 줄 요약을 쓸 것')
    for r in (stance or {}).get('strategies') or []:
        label = r.get('label', '?')
        pts = r.get('points') or []
        if len(pts) > 5:
            warn.append(f'stance {label} points {len(pts)}개 — 5개 이내로')
        if r.get('detail') and not pts:
            warn.append(f'stance {label}: 구형 detail 줄글 — points[] 점 목록으로')
        check(f'stance {label} 헤드라인', r.get('headline', ''))
        for p in pts:
            check(f'stance {label} 항목', p)
            if len(p) > 280:
                warn.append(f'stance {label} 항목 {len(p)}자 — 1~2문장으로 쪼갤 것: {p[:24]}…')

    for t in (triggers or {}).get('triggers') or []:
        tag = t.get('tag', '?')
        if t.get('status') not in ('hit', 'approaching', 'dormant'):
            warn.append(f'트리거 {tag}: status "{t.get("status")}" — hit·approaching·dormant 중 하나여야 한다')
        if not t.get('ko'):
            warn.append(f'트리거 {tag}: ko(우리말 풀이) 없음 — 영어 태그 옆에 병기할 한글 이름을 넣을 것')
        for fld, lim in (('cond', 300), ('act', 600), ('now', 400)):
            v = t.get(fld) or ''
            check(f'트리거 {tag}.{fld}', v)
            if len(v) > lim:
                warn.append(f'트리거 {tag}.{fld} {len(v)}자(>{lim}) — 판독을 이어붙이지 말고 덮어쓸 것')
        for a in t.get('acts') or []:
            check(f'트리거 {tag}.acts', a)
        gl = t.get('gloss')
        for g_ in ([gl] if isinstance(gl, str) else (gl or [])):
            check(f'트리거 {tag}.gloss', g_)
        if '| 이전:' in (t.get('cond') or '') + (t.get('now') or ''):
            warn.append(f'트리거 {tag}: "| 이전:" 꼬리 — 지난 판독은 보존하지 않는다(이력은 git·작업일지)')

    # 실적 일정: 출처 없는 일정은 만들지 않는다(지어내지 않음 원칙)
    for e in (earnings or {}).get('events') or []:
        nm = e.get('name', '?')
        if not re.match(r'\d{4}-\d{2}-\d{2}$', e.get('date') or ''):
            warn.append(f'실적 일정 {nm}: date 형식 오류(YYYY-MM-DD) — {e.get("date")!r}')
        if e.get('status') not in ('확정', '예상'):
            warn.append(f'실적 일정 {nm}: status는 "확정"|"예상" — {e.get("status")!r}')
        if not e.get('source'):
            warn.append(f'실적 일정 {nm}: source(출처) 없음 — 확인한 출처 없이 일정을 적지 않는다')

    for w in warn:
        print('⚠️ 작성 규칙 —', w)


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
    fomc = _load(FOMC_PATH, 'FOMC 일정')
    earnings = _load(EARN_PATH, '실적 발표 일정')
    screen = _load(SBX_PATH, '전략 B 스크리너')
    if not any((snap, us, kr, stance, triggers, usm, cal, screen, earnings)):
        print('주입할 소스가 하나도 없음 — 종료')
        return
    _warn_if_editorial_stale(us, snap)
    _lint_sources(us, kr, stance, triggers, (snap or {}).get('bas_dd'), earnings)
    with open(HTML_PATH, encoding='utf-8') as f:
        html = f.read()
    out = render(html, snap, us, kr, stance, triggers, usm, cal, screen, fomc, earnings)
    size = len(out.encode('utf-8'))
    if size > _HTML_SOFT_LIMIT:
        print(f'⚠️ 분량 경고 — HTML {size:,}바이트 > {_HTML_SOFT_LIMIT:,}: 일지식 덧붙임이 쌓였는지 확인할 것(BRIEFING_PIPELINE.md "본문 작성 규칙")')
    if out != html:
        with open(HTML_PATH, 'w', encoding='utf-8') as f:
            f.write(out)
        print(f'저장 완료: {HTML_PATH}')
    else:
        print('변경 없음.')


if __name__ == '__main__':
    main()
