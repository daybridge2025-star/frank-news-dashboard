"""
KRX 데이터 스냅샷 수집 — GitHub Actions에서 하루 1회 실행 (KST 06:20, 미국장 마감 후)
GitHub Actions: .github/workflows/krx_snapshot.yml

매크로 전략 브리핑(reports/macro-strategy-briefing.html) 작성 시 참고 자료로 쓴다.

데이터 소스 2개 병행 (2026-07-10 실측 검증):
  1. Open API(utils.krx, AUTH_KEY) — 헤드라인 지수 + 업종별 지수 + 종목 시세.
     서비스 이용신청 승인 후 동작. T+1이라 보통 전 영업일이 최신.
  2. pykrx(utils.krx_scrape, data.krx.co.kr):
     - 투자자별 수급(외국인/기관/개인/연기금) — 로그인(KRX_ID/KRX_PW) 필요.
     - 개별종목 시세 — 로그인 없이 동작 → Open API 미승인/미설정 시 폴백.

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
from utils.krx_scrape import get_investor_flow, get_stock_ohlcv

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


def _latest_business_day(auth_key):
    """
    최근 영업일 판별 — 오늘부터 최대 7일 역산.
    Open API 키가 있으면 지수 응답이 나오는 최신일(=공식 T+1 기준일)을 우선 채택,
    없으면 pykrx 개별종목 시세(무로그인)로 판별한다.
    반환: (기준일자 'YYYYMMDD', kospi_index_rows) — pykrx 경로면 index rows는 빈 리스트.
    """
    now = datetime.now(KST)
    if auth_key:
        for i in range(7):
            d = (now - timedelta(days=i)).strftime('%Y%m%d')
            rows = get_kospi_index(d, auth_key)
            if rows:
                return d, rows
    for i in range(7):
        d = (now - timedelta(days=i)).strftime('%Y%m%d')
        if get_stock_ohlcv(d, '005930'):  # pykrx, 로그인 없이 동작
            return d, []
    return now.strftime('%Y%m%d'), []


def main():
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')
    print(f'[{now_str}] KRX 데이터 스냅샷 수집 시작')
    print('=' * 50)

    auth_key = os.environ.get('KRX_AUTH_KEY', '')
    if not auth_key:
        print('KRX_AUTH_KEY 없음 — Open API는 건너뛰고 pykrx로만 수집(종목 시세만)')

    bas_dd, kospi_idx = _latest_business_day(auth_key)
    kosdaq_idx = get_kosdaq_index(bas_dd, auth_key) if auth_key else []
    print(f'기준일자: {bas_dd} | KOSPI 지수 {len(kospi_idx)}행 / KOSDAQ 지수 {len(kosdaq_idx)}행')

    # 종목: Open API 벌크 1회 호출 → 로컬 필터, 없으면 pykrx 폴백
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

    snapshot = {
        'bas_dd': bas_dd,
        'fetched_at': now_str,
        'index': {
            'KOSPI':  _norm_index(headline_index(kospi_idx, '코스피')),
            'KOSDAQ': _norm_index(headline_index(kosdaq_idx, '코스닥')),
        },
        'sectors': {
            'KOSPI':  _sectors(kospi_idx),
            'KOSDAQ': _sectors(kosdaq_idx),
        },
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
