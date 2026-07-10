"""
pykrx 기반 투자자별 수급 수집 (data.krx.co.kr)

배경 (2026-07-10 실측):
- KRX가 최근 data.krx.co.kr 통계 데이터를 로그인 기반으로 전환했다.
- pykrx 1.2.8 실측 결과: 개별종목 시세(OHLCV)는 로그인 없이 되지만,
  투자자별 거래대금·순매수 상위는 로그인(KRX_ID/KRX_PW)이 있어야 응답이 온다.
- 따라서 이 모듈은 무료 회원 계정(data.krx.co.kr)이 KRX_ID/KRX_PW 환경변수로
  주어졌을 때만 투자자 수급을 채운다. 없으면 데이터를 지어내지 않고 'unavailable'.

pykrx는 import 시점에 KRX_ID/KRX_PW 환경변수를 읽어 자동 로그인한다.
공식 라이브러리 함수만 호출하며, 엔드포인트를 추측·우회하지 않는다.
"""

import os

# 이 리포트의 투자자별 수급 표가 쓰는 4개 주체
INVESTORS = ['외국인', '기관합계', '개인', '연기금']


def has_credentials():
    return bool(os.environ.get('KRX_ID') and os.environ.get('KRX_PW'))


def _unavailable(reason):
    return {'status': 'unavailable', 'reason': reason}


def _import_stock():
    """pykrx는 import 시 자동 로그인. 실패해도 예외로 죽지 않게 감싼다."""
    try:
        from pykrx import stock
        return stock
    except Exception as e:
        print(f'[pykrx] import 실패: {e}')
        return None


def _native(v):
    """numpy/pandas 스칼라를 JSON 직렬화 가능한 파이썬 기본형으로 변환."""
    if hasattr(v, 'item'):          # numpy int64/float64 등
        try:
            return v.item()
        except Exception:
            pass
    if hasattr(v, 'strftime'):      # Timestamp
        return v.strftime('%Y-%m-%d')
    return v


def get_stock_ohlcv(bas_dd, code):
    """
    개별종목 일별 시세 — pykrx 실측상 로그인 없이도 동작(자격증명 없어도 채워짐).
    반환: {'종가':.., '거래량':.., ...} 또는 None
    """
    stock = _import_stock()
    if stock is None:
        return None
    try:
        df = stock.get_market_ohlcv(bas_dd, bas_dd, code)
        if df is None or df.empty:
            return None
        rec = df.reset_index().iloc[0].to_dict()
        return {str(k): _native(v) for k, v in rec.items()}
    except Exception as e:
        print(f'[pykrx] {code} 시세 실패: {e}')
        return None


def get_investor_value(bas_dd, market='KOSPI', fromdate=None):
    """
    시장 전체 투자자별 거래대금/순매수 (코스피/코스닥 투자자별 수급 표).
    fromdate 없으면 bas_dd 당일치. fromdate가 있으면 fromdate~bas_dd 기간 합계를
    KRX가 서버에서 이미 집계해 반환한다(실측 확인 — 6개월 범위도 1초 이내, 하루씩
    받아 우리가 더할 필요 없음).
    반환: [{'투자자구분': str, '매도':int, '매수':int, '순매수': int, ...}] 또는 None
    """
    stock = _import_stock()
    if stock is None:
        return None
    try:
        df = stock.get_market_trading_value_by_investor(fromdate or bas_dd, bas_dd, market)
        if df is None or df.empty:
            return None
        df = df.reset_index()
        records = df.to_dict(orient='records')
        return [{str(k): _native(v) for k, v in rec.items()} for rec in records]
    except Exception as e:
        print(f'[pykrx] {market} 투자자별 거래대금 실패 ({fromdate or bas_dd}~{bas_dd}): {e}')
        return None


def get_net_purchase_top(bas_dd, market='KOSPI', investor='외국인', top=5):
    """
    특정 투자자(외국인/연기금 등)의 종목별 순매수 → 매수·매도 상위 top개.
    반환: {'buy': [{'종목':..,'순매수':..}], 'sell': [...]} 또는 None
    """
    stock = _import_stock()
    if stock is None:
        return None
    try:
        df = stock.get_market_net_purchases_of_equities(bas_dd, bas_dd, market, investor)
        if df is None or df.empty:
            return None
        df = df.reset_index()
        # 순매수 컬럼명은 pykrx 버전에 따라 다를 수 있어 방어적으로 탐색
        col = next((c for c in df.columns if '순매수' in str(c) and '거래대금' in str(c)), None)
        if col is None:
            col = next((c for c in df.columns if '순매수' in str(c)), None)
        if col is None:
            return None
        name_col = next((c for c in df.columns if str(c) in ('종목명', '종목')), df.columns[0])
        df = df[[name_col, col]].sort_values(col, ascending=False)
        buy = [{'종목': r[name_col], '순매수': int(r[col])} for _, r in df.head(top).iterrows()]
        sell = [{'종목': r[name_col], '순매수': int(r[col])} for _, r in df.tail(top).iloc[::-1].iterrows()]
        return {'buy': buy, 'sell': sell}
    except Exception as e:
        print(f'[pykrx] {market}/{investor} 순매수 상위 실패: {e}')
        return None


def _period_bounds(bas_dd):
    """bas_dd 기준 이번달 초('YYYYMM01')·연초('YYYY0102') 문자열. 비영업일이어도
    범위 쿼리가 그 안의 실제 거래일만 집계하므로 문제없다(실측 확인)."""
    year, month = bas_dd[:4], bas_dd[:6]
    return month + '01', year + '0102'


def get_investor_flow(bas_dd):
    """
    스냅샷용 통합 진입점. 자격증명 없으면 unavailable을 명확히 반환.
    fetch_krx_snapshot.py가 이 함수의 반환을 investor_flow에 그대로 넣는다.
    """
    if not has_credentials():
        return _unavailable('KRX_ID/KRX_PW 미설정 — data.krx.co.kr 무료 회원가입 후 시크릿 등록 필요')

    mtd_from, ytd_from = _period_bounds(bas_dd)
    result = {'status': 'ok', 'bas_dd': bas_dd, 'mtd_from': mtd_from, 'ytd_from': ytd_from}
    for market in ('KOSPI', 'KOSDAQ'):
        result[market] = {
            'investor_value':      get_investor_value(bas_dd, market),
            'investor_value_mtd':  get_investor_value(bas_dd, market, fromdate=mtd_from),
            'investor_value_ytd':  get_investor_value(bas_dd, market, fromdate=ytd_from),
            'foreign_net_top':     get_net_purchase_top(bas_dd, market, '외국인'),
            'pension_net_top':     get_net_purchase_top(bas_dd, market, '연기금'),
        }
    # 전 섹션이 비면(로그인은 됐으나 데이터 0) 사실대로 낮춰 표기
    any_data = any(
        result[m][k] for m in ('KOSPI', 'KOSDAQ')
        for k in ('investor_value', 'investor_value_mtd', 'investor_value_ytd',
                   'foreign_net_top', 'pension_net_top')
    )
    if not any_data:
        return _unavailable('로그인은 됐으나 응답 비어있음 — 휴장일이거나 KRX 응답 형식 변경 가능')
    return result
