"""
KRX 데이터 스냅샷 수집 — GitHub Actions에서 하루 1회 실행 (KST 06:20, 미국장 마감 후)
GitHub Actions: .github/workflows/krx_snapshot.yml

매크로 전략 브리핑(reports/macro-strategy-briefing.html) 작성 시 참고 자료로 쓴다.

데이터 소스 우선순위 (2026-07-11 재설계 — pykrx 우선):
  1. pykrx(utils.krx_scrape, data.krx.co.kr) — **주 소스**.
     - 기준일 판정: 개별종목 시세(무로그인)가 장 마감 후 몇 시간 내 당일치를 제공 —
       가장 신선하므로 bas_dd는 항상 pykrx 기준으로 정한다.
     - 지수(코스피/코스닥)·업종 등락률·투자자 수급·순매수 상위: 로그인(KRX_ID/KRX_PW) 필요.
  2. Open API(utils.krx, AUTH_KEY) — **폴백 전용**. T+1 발행 시각이 새벽 브리핑보다
     늦다는 것이 실측 확인됨(7/11 07:28 미발행 → 19:12 발행). 따라서 기준일 판정에
     쓰지 않으며, pykrx가 실패한 필드를 같은 bas_dd 데이터가 있을 때만 메운다 —
     **bas_dd보다 오래된 날짜로 후퇴시키지 않는다.**

원칙: 자격증명/승인이 없으면 데이터를 지어내지 않고 '미확보'로 남긴다.
출력: data/krx_snapshot_{YYYYMMDD}.json + data/krx_snapshot_latest.json
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

# Windows 로컬 콘솔은 기본 cp949라 '—' 등 유니코드 문자에서 죽는다 — UTF-8로 강제
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import json
from datetime import datetime, timedelta
import pytz

from utils.krx import (
    get_kospi_index, get_kosdaq_index, get_kospi_stocks,
    headline_index, stocks_by_code,
)
from utils.krx_scrape import (
    get_investor_flow, get_stock_ohlcv, get_index_summary, has_credentials,
)

KST = pytz.timezone('Asia/Seoul')

# 전략 A/B 한국 20% 배분에 등장하는 관심종목 (reports/macro-strategy-briefing.html 기준)
WATCHLIST = {
    '005930': '삼성전자',
    '000660': 'SK하이닉스',
    '267260': 'HD현대일렉트릭',
    '010120': 'LS ELECTRIC',
    '012450': '한화에어로스페이스',
    '064350': '현대로템',
}


def _num(x):
    """'1,234' / '278000' / 12.3 → int 또는 float. 실패 시 None."""
    if x is None:
        return None
    s = str(x).replace(',', '').strip()
    if s in ('', '-'):
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def _norm_openapi_stock(r):
    return {
        'close':      _num(r.get('TDD_CLSPRC')),
        'change_pct': _num(r.get('FLUC_RT')),
        'open':       _num(r.get('TDD_OPNPRC')),
        'high':       _num(r.get('TDD_HGPRC')),
        'low':        _num(r.get('TDD_LWPRC')),
        'volume':     _num(r.get('ACC_TRDVOL')),
    }


def _norm_pykrx_stock(o):
    return {
        'close':      _num(o.get('종가')),
        'change_pct': _num(o.get('등락률')),
        'open':       _num(o.get('시가')),
        'high':       _num(o.get('고가')),
        'low':        _num(o.get('저가')),
        'volume':     _num(o.get('거래량')),
    }


def _norm_index(r):
    if not r:
        return None
    return {
        'name':       r.get('IDX_NM'),
        'close':      _num(r.get('CLSPRC_IDX')),
        'change_pct': _num(r.get('FLUC_RT')),
        'open':       _num(r.get('OPNPRC_IDX')),
        'high':       _num(r.get('HGPRC_IDX')),
        'low':        _num(r.get('LWPRC_IDX')),
    }


def _sectors(rows):
    """지수 전체 리스트를 [{name, change_pct, close}]로 정규화(업종별 등락률 표용)."""
    out = []
    for r in rows or []:
        out.append({
            'name':       r.get('IDX_NM'),
            'change_pct': _num(r.get('FLUC_RT')),
            'close':      _num(r.get('CLSPRC_IDX')),
        })
    return out


def _latest_business_day():
    """
    최근 영업일 판별 — 오늘부터 최대 7일 역산, **pykrx 개별종목 시세(무로그인) 기준**.
    pykrx는 장 마감 후 몇 시간 내 당일치를 제공하므로 가장 신선하다. Open API는 T+1
    발행이 새벽 브리핑보다 늦어 기준일 판정에 쓰면 하루 이상 후퇴한다(7/11 실측).
    반환: 'YYYYMMDD' 또는 None.
    None은 pykrx가 최근 7일 내내 무응답이라는 뜻 — 소스 차단·장애로 간주한다.
    이 경우 호출자는 절대 '오늘 날짜'로 임의 대체하면 안 된다 — 실제로는 이전
    영업일 데이터인 필드들에 오늘 날짜 라벨을 붙이는 날짜 불일치가 생기기 때문이다.
    """
    now = datetime.now(KST)
    for i in range(7):
        d = (now - timedelta(days=i)).strftime('%Y%m%d')
        if get_stock_ohlcv(d, '005930'):
            return d
    return None


def _print_kospi_buy_trigger_hint(close, flow):
    """
    data/triggers.json의 'Add — KOSPI 7,000~7,200 + 외국인 순매수 전환' 트리거는
    이 파이프라인이 이미 수집하는 두 수치만으로 판정 가능한 유일한 조건이라
    참고용 콘솔 힌트를 남긴다. status는 여전히 triggers.json에서 사람이 갱신 —
    여기서는 절대 자동으로 바꾸지 않는다(1~2일 지속 확인 등 판단이 더 필요하므로).
    close: 정규화된 코스피 종가(float) 또는 None.
    """
    iv = (flow.get('KOSPI') or {}).get('investor_value') if isinstance(flow, dict) else None
    foreign_net = None
    for r in iv or []:
        if r.get('투자자구분') == '외국인':
            foreign_net = r.get('순매수')
            break
    if close is None or foreign_net is None:
        return
    in_range = 7000 <= close <= 7200
    buying = foreign_net > 0
    verdict = '조건 충족' if (in_range and buying) else ('조건 일부 근접' if (in_range or buying) else '조건 미충족')
    print(f'🔔 KOSPI 매수 트리거 힌트: {verdict} — 코스피 {close:,.0f}'
          f'({"구간 내" if in_range else "구간 밖"}) · 외국인 전일 {foreign_net / 1e8:+,.0f}억'
          f'({"순매수" if buying else "순매도"}). data/triggers.json의 status는 참고 후 직접 갱신.')


def main():
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')
    print(f'[{now_str}] KRX 데이터 스냅샷 수집 시작')
    print('=' * 50)

    auth_key = os.environ.get('KRX_AUTH_KEY', '')
    if not auth_key:
        print('KRX_AUTH_KEY 없음 — Open API 폴백 없이 pykrx로만 수집')

    bas_dd = _latest_business_day()
    if bas_dd is None:
        print('오류: pykrx가 최근 7일 데이터를 하나도 못 찾음 — 소스 차단/장애로 판단.')
        print('기존 스냅샷을 잘못된 날짜로 덮어쓰지 않기 위해 아무것도 저장하지 않고 종료한다.')
        sys.exit(1)
    print(f'기준일자: {bas_dd} (pykrx 판정)')

    # ── 지수·업종: pykrx 우선(로그인 필요) → 같은 bas_dd의 Open API로만 폴백 ──
    index, sectors, idx_src = {}, {}, {}
    for market in ('KOSPI', 'KOSDAQ'):
        headline, secs = (get_index_summary(bas_dd, market)
                          if has_credentials() else (None, None))
        if headline:
            index[market], sectors[market], idx_src[market] = headline, secs, 'pykrx'
        else:
            rows = ((get_kospi_index if market == 'KOSPI' else get_kosdaq_index)
                    (bas_dd, auth_key) if auth_key else [])
            if rows:  # Open API가 bas_dd 당일치를 이미 발행한 경우에만 채워짐
                index[market] = _norm_index(headline_index(rows, '코스피' if market == 'KOSPI' else '코스닥'))
                sectors[market], idx_src[market] = _sectors(rows), 'openapi'
            else:
                index[market], sectors[market], idx_src[market] = None, None, 'none'
        n = len(sectors[market] or [])
        print(f'  {market} 지수·업종: {n}행 [{idx_src[market]}]')

    # ── 종목: Open API 벌크(있으면) → pykrx 폴백(무로그인) ──
    by_code = stocks_by_code(get_kospi_stocks(bas_dd, auth_key)) if auth_key else {}
    stocks = {}
    for code, name in WATCHLIST.items():
        row = by_code.get(code)
        if row:
            stocks[code] = {'name': name, 'source': 'openapi', **_norm_openapi_stock(row)}
            src = 'openapi'
        else:
            ohlcv = get_stock_ohlcv(bas_dd, code)  # pykrx, 무로그인
            if ohlcv:
                stocks[code] = {'name': name, 'source': 'pykrx', **_norm_pykrx_stock(ohlcv)}
                src = 'pykrx'
            else:
                stocks[code] = {'name': name, 'source': 'none'}
                src = 'none'
        px = stocks[code].get('close')
        print(f"  {name}({code}): {px} [{src}]")

    flow = get_investor_flow(bas_dd)  # pykrx (KRX_ID/PW 있을 때만 채워짐)
    print(f"투자자 수급: {flow.get('status')}"
          + (f" — {flow.get('reason')}" if flow.get('status') != 'ok' else ''))

    _print_kospi_buy_trigger_hint((index.get('KOSPI') or {}).get('close'), flow)

    snapshot = {
        'bas_dd': bas_dd,
        'fetched_at': now_str,
        'index': index,
        'sectors': sectors,
        'stocks': stocks,
        'investor_flow': flow,
    }

    os.makedirs('data', exist_ok=True)
    dated_path = f'data/krx_snapshot_{bas_dd}.json'
    latest_path = 'data/krx_snapshot_latest.json'
    for path in (dated_path, latest_path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)

    print('=' * 50)
    print(f'완료: {dated_path}, {latest_path} 저장')


if __name__ == '__main__':
    main()
