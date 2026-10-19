"""
브리핑 본문 조립 부품 — '문체·형식 가이드'(2026-10-19, BRIEFING_PIPELINE.md)를 코드로 고정한다.

편집 소스(JSON)는 가벼운 마크업만 쓴다: `**굵게**` → <b>, `==형광펜==` → <mark>. 그 밖의 HTML은 전부 이스케이프되므로
소스가 HTML을 주입할 수 없다. 이슈 카드·스탠스·트리거 카드·일정 박스를 이 모듈이 그린다.

구형(v1) 소스도 계속 렌더링한다 — 이슈 카드 {title, desc[], impacts[]}, 스탠스 {headline, points[]},
트리거 {cond, act, now}. 단 구형 문장 앞의 이모지는 떼고(문장 앞 이모지 금지 규칙), 문체 린트가 경고한다.

이 모듈은 표준 라이브러리만 쓴다(Action 러너에서 requests 없이도 import 가능해야 함).
"""

import html as _html
import re
from datetime import date, datetime

_WD = '월화수목금토일'


def esc(s):
    return _html.escape(str(s), quote=False)


# ── 인라인 마크업 ────────────────────────────────────────────
_BOLD = re.compile(r'\*\*(.+?)\*\*')
_MARK = re.compile(r'==(.+?)==')
# 문장 앞 이모지·기호(그림문자, 국기, 딩뱃, 변형선택자, ZWJ)와 뒤따르는 공백
_LEAD_EMOJI = re.compile('^[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF☀-➿⭐️‍\\s]+')
_ANY_EMOJI = re.compile('[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF☀-➿⭐️]')


def strip_lead_emoji(s):
    return _LEAD_EMOJI.sub('', str(s or '')).strip()


def rich(s):
    """이스케이프 후 **굵게**·==형광펜== 만 태그로 바꾼다."""
    t = esc(s)
    t = _BOLD.sub(r'<b>\1</b>', t)
    t = _MARK.sub(r'<mark>\1</mark>', t)
    return t


def plain(s):
    """마크업 기호를 떼고 글자만(문체 점검·길이 계산용)."""
    return _MARK.sub(r'\1', _BOLD.sub(r'\1', str(s or '')))


def kdate(d, with_wd=True):
    """date | 'YYYY-MM-DD' | 'YYYYMMDD' → '10월 19일(월)'. 해석 못 하면 None."""
    if isinstance(d, str):
        m = re.match(r'(\d{4})-?(\d{2})-?(\d{2})', d)
        if not m:
            return None
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if not isinstance(d, date):
        return None
    return f'{d.month}월 {d.day}일' + (f'({_WD[d.weekday()]})' if with_wd else '')


# ── 번호 배지 목록 ───────────────────────────────────────────
def num_rows(items, cls='row'):
    """['…','…'] → 번호 배지(1·2·3)가 붙은 줄들. 줄이 길어져도 들여쓰기가 걸리도록 flex 행으로 그린다."""
    return ''.join(
        f'<div class="{cls}"><span class="nb">{i}</span><span>{rich(strip_lead_emoji(t))}</span></div>'
        for i, t in enumerate(items, 1) if str(t or '').strip())


_CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩'
_CIRCLED_SPLIT = re.compile('[' + _CIRCLED + ']')


def split_circled(s):
    """'① 가 ② 나' 처럼 한 문자열에 이어붙은 번호 항목을 나눈다. 번호가 없으면 [s]."""
    s = (s or '').strip()
    if not s:
        return []
    if not _CIRCLED_SPLIT.search(s):
        return [s]
    parts = [p.strip(' ·;') for p in _CIRCLED_SPLIT.split(s)]
    return [p for p in parts if p]


# ── 이슈 카드 ────────────────────────────────────────────────
_LEVEL_COLOR = {'g': 'var(--good)', 'w': 'var(--warn)', 'c': 'var(--crit)', 's': 'var(--serious)'}


def _impacts(impacts):
    lis = ''
    for imp in impacts or []:
        lvl = imp.get('level', 'w')
        col = _LEVEL_COLOR.get(lvl, _LEVEL_COLOR['w'])
        lis += (f'<li><span class="dot" style="background:{col}"></span>'
                f'<b>{esc(imp.get("strategy", ""))}:</b> {rich(strip_lead_emoji(imp.get("text", "")))}</li>')
    return f'<h4 class="sec">전략별로는 이렇게 봐요</h4><ul class="lbl">{lis}</ul>' if lis else ''


def issue_card(it, tag, meta=None):
    """이슈 카드 1장. v2: {title, lead, sections[{heading, body[], checks[], note}], impacts[], sources}.
    v1: {title, desc[], impacts[]} — desc는 문단으로 그린다(이모지는 뗀다).
    tag: '#미국장 마감' 같은 알약 라벨, meta: 제목 아래 기준 문구(예: '10월 19일(월) 마감 기준')."""
    out = [f'<div class="art"><span class="tagpill">{esc(tag)}</span>',
           f'<h3 class="at">{rich(it.get("title", ""))}</h3>']
    if meta:
        out.append(f'<div class="ameta">{esc(meta)}</div>')
    if it.get('lead'):
        out.append(f'<p class="lead">{rich(it["lead"])}</p>')
    secs = it.get('sections') or []
    for s in secs:
        if s.get('heading'):
            out.append(f'<h4 class="sec">{rich(s["heading"])}</h4>')
        for p in s.get('body') or []:
            out.append(f'<p class="abody">{rich(p)}</p>')
        chk = [c for c in (s.get('checks') or []) if str(c or '').strip()]
        if chk:
            out.append('<div class="checks abody"><div class="ch">확인할 것</div>' + num_rows(chk) + '</div>')
        notes = s.get('note')
        for n in ([notes] if isinstance(notes, str) else (notes or [])):
            out.append(f'<p class="fn">{rich(n)}</p>')
    if not secs:                                   # v1 호환: desc 문단
        for d in it.get('desc') or []:
            out.append(f'<p class="abody">{rich(strip_lead_emoji(d))}</p>')
    out.append(_impacts(it.get('impacts')))
    if it.get('sources'):
        out.append(f'<div class="asrc">출처: {rich(it["sources"])}</div>')
    out.append('</div>')
    return ''.join(out)


# ── 스탠스 ──────────────────────────────────────────────────
def stance_row(r):
    """strategies[i] 1건 → 전략 이름 알약 + 한 문장 헤드라인 + 짧은 근거들(점 목록).
    points가 없으면 구형 detail(줄글)을 한 문단으로."""
    pts = [p for p in (r.get('points') or []) if str(p or '').strip()]
    if pts:
        body = '<ul class="spl">' + ''.join(f'<li>{rich(strip_lead_emoji(p))}</li>' for p in pts) + '</ul>'
    elif r.get('detail'):
        body = f'<p class="sp2">{rich(r["detail"])}</p>'
    else:
        body = ''
    return (f'<div class="srow2"><div class="sh"><span class="sb">{esc(r.get("label", ""))}</span>'
            f'<span class="sv2">{rich(strip_lead_emoji(r.get("headline", "")))}</span></div>{body}</div>')


def stance_all(stance):
    rows = (stance or {}).get('strategies') or []
    return ''.join(stance_row(r) for r in rows) or None


def stance_single(stance, letter):
    for r in (stance or {}).get('strategies') or []:
        if (r.get('label') or '').strip().upper().startswith(letter):
            return f'<div class="stance2"><span class="tagpill">#오늘의 스탠스</span>{stance_row(r)}</div>'
    return None


# ── 트리거 카드 ──────────────────────────────────────────────
_STAT = {'hit': ('발동', 'st-hit'), 'approaching': ('임박', 'st-near'), 'dormant': ('대기', 'st-idle')}
_STATUS_ORDER = {'hit': 0, 'approaching': 1, 'dormant': 2}


def trigger_cards(triggers):
    rows = (triggers or {}).get('triggers') or []
    if not rows:
        return None
    cards = []
    for t in sorted(rows, key=lambda x: _STATUS_ORDER.get(x.get('status'), 2)):
        cat = t.get('category', 'watch')
        if cat not in ('buy', 'sell', 'watch'):
            cat = 'watch'
        label, cls = _STAT.get(t.get('status'), _STAT['dormant'])
        ko = f'<small>{esc(t["ko"])}</small>' if t.get('ko') else ''
        strat = ''.join(f'<span class="sp-pill">{esc(s)}</span>' for s in t.get('strategies', []))
        head = (f'<div class="th"><span class="stat {cls}">{label}</span>'
                f'<span class="tn">{esc(t.get("tag", ""))}{ko}</span>{strat}</div>')
        cond = f'<p class="tc">{rich(strip_lead_emoji(t.get("cond", "")))}</p>' if t.get('cond') else ''
        now = (t.get('now') or '').strip()
        now_html = (f'<div class="tnow"><span class="nl">지금은</span>{rich(strip_lead_emoji(now))}</div>'
                    if now else '')
        acts = t.get('acts')
        if not acts:
            acts = split_circled(t.get('act'))
        if len(acts) > 1:
            act_html = '<div class="tact"><div class="al">켜지면 이렇게 해요</div>' + num_rows(acts) + '</div>'
        elif acts:
            act_html = (f'<div class="tact"><div class="al">켜지면 이렇게 해요</div>'
                        f'<p class="ap">{rich(strip_lead_emoji(acts[0]))}</p></div>')
        else:
            act_html = ''
        gl = t.get('gloss')
        gloss = ''.join(f'<p class="fn">{rich(g)}</p>' for g in ([gl] if isinstance(gl, str) else (gl or [])))
        cards.append(f'<div class="trg2 {cat}">{head}{cond}{now_html}{act_html}{gloss}</div>')
    return '\n  ' + '\n  '.join(cards) + '\n  '


# ── 일정 박스 ────────────────────────────────────────────────
def _li(date_txt, body, small=''):
    return f'<li><b>{esc(date_txt)}:</b> {esc(body)}{(" <small>" + esc(small) + "</small>") if small else ""}</li>'


def sched_box(cal, fomc_meetings, earnings, ref, ref_kr=None):
    """앞으로 볼 일정 박스 안쪽 HTML — 경제 지표·연준(미국 현지 날짜) + 실적 발표(한국시간). 둘 다 비면 ''(박스를 숨긴다).
    ref: 미국 현지 기준일(date), ref_kr: 한국 기준일(date, 없으면 ref) — 지난 일정은 걸러낸다.
    시계가 아니라 수집 시각(fetched_at)에서 받아 언제 다시 빌드해도 같은 결과."""
    ref_kr = ref_kr or ref
    groups = []

    # 1) 경제 지표·연준
    econ = []
    stale = ''
    if cal:
        evs = sorted((cal.get('events') or []), key=lambda e: (e.get('date', ''), e.get('kind', '')))
        by_date = {}
        for e in evs:
            by_date.setdefault(e.get('date', ''), []).append(e.get('name', ''))
        for d, names in by_date.items():
            dt = kdate(d)
            if dt and (ref is None or d >= ref.isoformat()):
                econ.append((d, dt, ' · '.join(n for n in names if n)))
        fred_ok = cal.get('fred_ok', cal.get('status') == 'ok')
        if cal.get('stale_events') and econ:
            when = (cal.get('events_fetched_at') or '')[:16].replace('T', ' ')
            stale = f'FRED 응답이 늦어 {when}(UTC)에 받은 일정이에요.'
        elif not fred_ok:
            stale = '미국 지표 발표 일정은 아직 못 가져왔어요(FRED 응답 없음).'
    # 다음 FOMC — 이번 주 목록에 없어도 가까우면(3주 이내) 알려준다
    if fomc_meetings and ref is not None:
        for m in fomc_meetings:
            end = m.get('end') or ''
            if not end or end < ref.isoformat():
                continue
            try:
                gap = (date.fromisoformat(end) - ref).days
            except ValueError:
                continue
            if gap > 21:
                break
            start = m.get('start') or ''
            span = (f'{int(start[5:7])}월 {int(start[8:10])}~{int(end[8:10])}일'
                    if start[5:7] == end[5:7] and start else kdate(end, False))
            if not any(e[0] == end for e in econ):
                econ.append((end, span, '연준(FOMC) 금리 결정'))
            break
    if econ or stale:
        econ.sort(key=lambda x: x[0])
        items = ''.join(_li(dt, body) for _, dt, body in econ)
        note = f'<p class="fn" style="margin:2px 0 8px">{esc(stale)}</p>' if stale else ''
        groups.append('<div class="sg">경제 지표·연준 (미국 현지 날짜 기준)</div>'
                      + (f'<ul>{items}</ul>' if items else '') + note)

    # 2) 실적 발표(한국시간)
    erows = []
    for e in (earnings or {}).get('events') or []:
        d = e.get('date') or ''
        if not d or (ref_kr is not None and d < ref_kr.isoformat()):
            continue
        erows.append(e)
    erows.sort(key=lambda e: (e.get('date', ''), e.get('time', '')))
    if erows:
        merged = []                                 # 같은 날짜·시각은 한 줄로 묶는다
        for e in erows:
            key = (e.get('date'), e.get('time', ''))
            tags = (['보유'] if e.get('kind') == '보유' else []) + ([] if e.get('status', '확정') == '확정' else ['예정'])
            nm = e.get('name', '') + (f'({"·".join(tags)})' if tags else '')
            if merged and merged[-1][0] == key:
                merged[-1][1].append(nm)
            else:
                merged.append((key, [nm]))
        items = ''
        for (d, tm), names in merged:
            dt = kdate(d) or d
            items += _li(f'{dt} {tm}'.strip(), ' · '.join(names))
        note = '미국 기업은 현지 장 마감 뒤에 발표해서 한국시간으로는 다음 날 새벽이에요.'
        if any(e.get('kind') == '보유' for e in erows):
            note += ' (보유)는 JM님 계좌에 있는 종목이에요.'
        if any(e.get('status', '확정') != '확정' for e in erows):
            note += ' (예정)은 회사가 아직 날짜를 공지하지 않은 일정이에요.'
        groups.append(f'<div class="sg">실적 발표 (한국시간 기준)</div><ul>{items}</ul>'
                      f'<p class="fn" style="margin:2px 0 4px">{esc(note)}</p>')

    if not groups:
        return ''
    return ('<div class="st"><span class="ckico">✓</span>앞으로 볼 일정</div>' + ''.join(groups))


# ── 문체 점검(경고만) ────────────────────────────────────────
_EN_STATUS = re.compile(r'\b(hit|approaching|dormant)\b', re.I)
_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+')
_DA_END = re.compile(r'다[.)"\'”’]*$')


def style_warnings(where, text, max_sentence=90, max_para=240):
    """독자 노출 문장 1개 필드의 문체 위반을 문자열 목록으로 돌려준다(경고 전용).
    해요체(~해요·~예요·~죠), 한 문장 90자 안팎, 이모지·영어 상태어·대시 남발 금지."""
    t = plain(text).strip()
    if not t:
        return []
    out = []
    if _ANY_EMOJI.search(t):
        out.append(f'{where}: 이모지 금지(문장 앞 이모지는 쓰지 않는다)')
    m = _EN_STATUS.search(t)
    if m:
        out.append(f'{where}: 영어 상태어 "{m.group(0)}" — 화면의 알약과 같은 우리말로(hit→발동·approaching→임박·dormant→대기)')
    sents = [s for s in _SENT_SPLIT.split(t) if s.strip()]
    da = [s for s in sents if _DA_END.search(s.strip())]
    if da:
        out.append(f'{where}: "~다" 체 문장 {len(da)}개 — 해요체로(예: "{da[0][-18:]}")')
    if t.count('—') > 1:
        out.append(f'{where}: 대시(—) {t.count("—")}개 — 문장을 나눠 쓴다')
    long_s = [s for s in sents if len(s) > max_sentence]
    if long_s:
        out.append(f'{where}: {max_sentence}자 넘는 문장 {len(long_s)}개 — 쪼갤 것: {long_s[0][:22]}…')
    if len(t) > max_para:
        out.append(f'{where}: 한 문단 {len(t)}자 — {max_para}자 안팎으로')
    if t.count('(') > 3:
        out.append(f'{where}: 괄호 {t.count("(")}개 — 출처·부연은 각주나 출처 줄로')
    return out
