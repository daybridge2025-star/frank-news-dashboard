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
import time

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


_HEADLINE_NAME = {'KOSPI': '코스피', 'KOSDAQ': '코스닥'}
_INDEX_TICKER = {'KOSPI': '1001', 'KOSDAQ': '2001'}


def _find_col(df, *keywords):
    """컬럼명을 방어적으로 탐색 — pykrx 버전에 따라 이름이 다를 수 있다."""
    for c in df.columns:
        s = str(c)
        if all(k in s for k in keywords):
            return c
    return None


def get_index_summary(bas_dd, market='KOSPI'):
    """
    해당 시장의 전체 지수 등락 요약 — pykrx get_index_price_change(로그인 필요).
    한 번의 호출로 헤드라인(코스피/코스닥)과 업종지수 전체의 종가·등락률을 얻는다.
    반환: (headline_dict|None, sectors_list|None)
      headline = {'name','close','change_pct','open','high','low'} (high/low는 별도 보강)
      sectors  = [{'name','change_pct','close'}, ...]  (헤드라인 포함 전체 — 필터는 렌더러 몫)
    """
    stock = _import_stock()
    if stock is None:
        return None, None
    try:
        df = stock.get_index_price_change(bas_dd, bas_dd, market)
        if df is None or df.empty:
            return None, None
        df = df.reset_index()
        name_col = df.columns[0]
        close_col = _find_col(df, '종가')
        chg_col = _find_col(df, '등락률')
        open_col = _find_col(df, '시가')
        if close_col is None or chg_col is None:
            print(f'[pykrx] {market} 지수요약: 컬럼 인식 실패 — {list(df.columns)}')
            return None, None
        sectors, headline = [], None
        for _, r in df.iterrows():
            item = {'name': str(r[name_col]).strip(),
                    'close': _native(r[close_col]),
                    'change_pct': _native(r[chg_col])}
            sectors.append(item)
            if item['name'] == _HEADLINE_NAME[market]:
                headline = dict(item)
                if open_col is not None:
                    headline['open'] = _native(r[open_col])
        if headline:
            hl = get_index_ohlcv_day(bas_dd, _INDEX_TICKER[market])
            if hl:
                headline.setdefault('open', hl.get('open'))
                headline['high'] = hl.get('high')
                headline['low'] = hl.get('low')
        return headline, sectors
    except Exception as e:
        print(f'[pykrx] {market} 지수요약 실패: {e}')
        return None, None


def get_index_ohlcv_day(bas_dd, ticker):
    """지수 일봉 1일치(고가/저가 보강용) — 로그인 필요. 실패 시 None."""
    stock = _import_stock()
    if stock is None:
        return None
    try:
        df = stock.get_index_ohlcv(bas_dd, bas_dd, ticker)
        if df is None or df.empty:
            return None
        flat = df.reset_index()
        r = flat.iloc[0]
        out = {}
        for key, kw in (('open', '시가'), ('high', '고가'), ('low', '저가')):
            col = _find_col(flat, kw)
            out[key] = _native(r[col]) if col is not None else None
        return out
    except Exception as e:
        print(f'[pykrx] 지수 {ticker} 일봉 실패: {e}')
        return None


def get_index_ytd(bas_dd, ticker):
    """지수 연초 종가·연중(장중) 고점 — KOSPI 위치 바용. 로그인 필요, 실패 시 None."""
    stock = _import_stock()
    if stock is None:
        return None
    try:
        df = stock.get_index_ohlcv(f'{bas_dd[:4]}0102', bas_dd, ticker)
        if df is None or df.empty:
            return None
        close_col = _find_col(df, '종가')
        high_col = _find_col(df, '고가')
        if close_col is None:
            return None
        out = {'ytd_start': _native(df.iloc[0][close_col])}
        out['ytd_high'] = _native(df[high_col].max()) if high_col is not None else None
        return out
    except Exception as e:
        print(f'[pykrx] 지수 {ticker} YTD 실패: {e}')
        return None


def get_index_change_range(fromdate, todate, market='KOSPI'):
    """기간 지수 등락률 {지수명: pct} — 업종 '연초 대비' 컬럼용. 로그인 필요, 실패 시 빈 dict."""
    stock = _import_stock()
    if stock is None:
        return {}
    try:
        df = stock.get_index_price_change(fromdate, todate, market)
        if df is None or df.empty:
            return {}
        df = df.reset_index()
        name_col = df.columns[0]
        chg_col = _find_col(df, '등락률')
        if chg_col is None:
            return {}
        return {str(r[name_col]).strip(): _native(r[chg_col]) for _, r in df.iterrows()}
    except Exception as e:
        print(f'[pykrx] {market} 기간등락({fromdate}~{todate}) 실패: {e}')
        return {}


def get_sector_leaders(bas_dd, market='KOSPI', sector_names=None, top_n=3, pause=0.35):
    """업종지수별 대표종목(시가총액 상위 N)과 당일 등락 — {업종명: [{'name','chg_pct'}, ...]}.
    지수 구성종목(PDF)·시총·등락 전부 로그인 필요. 실패한 업종은 결과에서 빠질 뿐 전체를 죽이지 않는다.
    PDF 호출이 업종 수만큼(≈24/22회) 발생하므로 pause로 완충 — 과거 레이트리밋 사례 감안."""
    stock = _import_stock()
    if stock is None:
        return {}
    want = set(sector_names or [])
    try:
        tickers = stock.get_index_ticker_list(bas_dd, market=market)
        name_of = {t: str(stock.get_index_ticker_name(t)).strip() for t in tickers}
        cap = stock.get_market_cap(bas_dd, market=market)
        cap_col = _find_col(cap, '시가총액')
        caps = cap[cap_col].to_dict() if cap_col is not None else {}
        pc = stock.get_market_price_change(bas_dd, bas_dd, market=market)
        nm_col = _find_col(pc, '종목명')
        pchg_col = _find_col(pc, '등락률')
        stknm = pc[nm_col].to_dict() if nm_col is not None else {}
        stkchg = pc[pchg_col].to_dict() if pchg_col is not None else {}
    except Exception as e:
        print(f'[pykrx] {market} 대표종목 준비 실패: {e}')
        return {}
    out = {}
    for t, nm in name_of.items():
        if want and nm not in want:
            continue
        try:
            try:
                pdf = stock.get_index_portfolio_deposit_file(t)
            except TypeError:  # pykrx 버전에 따라 (date, ticker) 시그니처
                pdf = stock.get_index_portfolio_deposit_file(bas_dd, t)
            cons = [c for c in list(pdf or []) if c in caps]
            cons.sort(key=lambda c: caps.get(c) or 0, reverse=True)
            leaders = [{'name': str(stknm.get(c) or c),
                        'chg_pct': _native(stkchg[c]) if c in stkchg else None}
                       for c in cons[:top_n]]
            if leaders:
                out[nm] = leaders
        except Exception as e:
            print(f'[pykrx] {market} "{nm}" 구성종목 실패: {e}')
        time.sleep(pause)
    print(f'[pykrx] {market} 대표종목 확보: {len(out)}/{len(want) or len(name_of)}개 업종')
    return out


def probe_sector_investor_value(bas_dd):
    """업종지수 티커 단위 투자자별 거래대금이 pykrx로 되는지 1회 실측용 프로브.
    결과를 스냅샷 JSON에 실어 커밋되게 하면, Action 로그 접근 없이도 판단할 수 있다.
    지원이 확인되면 '업종별 외국인·기관 순매수' 기능으로 승격 예정 — 그 전까진 관측만."""
    stock = _import_stock()
    if stock is None:
        return {'status': 'no_pykrx'}
    out = {}
    try:
        tickers = stock.get_index_ticker_list(bas_dd, market='KOSPI')
        tmap = {str(stock.get_index_ticker_name(t)).strip(): t for t in tickers}
    except Exception as e:
        return {'status': f'ticker_list_error: {e}'}
    for label in ('코스피', '전기전자'):
        tk = tmap.get(label)
        if not tk:
            out[label] = {'status': 'no_ticker'}
            continue
        try:
            df = stock.get_market_trading_value_by_investor(bas_dd, bas_dd, tk)
            if df is None or df.empty:
                out[label] = {'status': 'empty', 'ticker': tk}
            else:
                d = df.reset_index()
                out[label] = {'status': 'ok', 'ticker': tk,
                              'columns': [str(c) for c in d.columns],
                              'rows': [{str(k): _native(v) for k, v in r.items()}
                                       for r in d.head(4).to_dict('records')]}
        except Exception as e:
            out[label] = {'status': f'error: {e}', 'ticker': tk}
        time.sleep(0.3)
    return out


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
