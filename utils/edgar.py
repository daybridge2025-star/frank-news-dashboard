"""
utils/edgar.py — SEC EDGAR XBRL 재무 데이터 추출 모듈
────────────────────────────────────────────────────────
주요 함수:
  get_cik(ticker)               → CIK 문자열 (10자리 패딩)
  get_company_facts(cik)        → companyfacts JSON (전체 XBRL)
  extract_annual_value(facts, tag, unit, years=3) → 최근 연간 값
  get_edgar_fundamentals(ticker, finnhub_api_key) → 계산용 dict

반환 dict 키:
  ebit, da, ebitda, debt, cash, equity, capex, tax_expense,
  pretax_income, interest_expense, shares_outstanding,
  tax_rate, nopat, invested_capital, roic,
  ev, ev_ebitda, fcf,
  industry (Finnhub profile2 → Damodaran 업종 매핑용)
"""

import requests
import time

EDGAR_BASE    = 'https://data.sec.gov'
EDGAR_HEADERS = {
    'User-Agent': 'FrankNewsDashboard daybridge2025@gmail.com',
    'Accept-Encoding': 'gzip, deflate',
}


# ── CIK 조회 ────────────────────────────────────────────────────
_cik_cache: dict = {}

def get_cik(ticker: str) -> str | None:
    """티커 → CIK(10자리). EDGAR company_tickers.json 사용."""
    ticker = ticker.upper().strip()
    if ticker in _cik_cache:
        return _cik_cache[ticker]
    try:
        r = requests.get(
            f'{EDGAR_BASE}/files/company_tickers.json',
            headers=EDGAR_HEADERS, timeout=10
        )
        if not r.ok:
            return None
        data = r.json()
        for item in data.values():
            if item.get('ticker', '').upper() == ticker:
                cik = str(item['cik_str']).zfill(10)
                _cik_cache[ticker] = cik
                return cik
    except Exception as e:
        print(f'[EDGAR] get_cik({ticker}) 오류: {e}')
    return None


# ── companyfacts 전체 로드 ───────────────────────────────────────
def get_company_facts(cik: str) -> dict | None:
    """
    SEC EDGAR companyfacts API → 전체 XBRL 재무 데이터.
    주의: 파일 크기 수 MB, Streamlit 캐시(@st.cache_data ttl=86400) 권장.
    """
    try:
        r = requests.get(
            f'{EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik}.json',
            headers=EDGAR_HEADERS, timeout=20
        )
        if r.ok:
            return r.json()
    except Exception as e:
        print(f'[EDGAR] get_company_facts({cik}) 오류: {e}')
    return None


# ── XBRL 태그에서 최신 연간값 추출 ──────────────────────────────
def extract_annual_value(facts: dict, tag: str, unit: str = 'USD',
                          years: int = 1) -> float | None:
    """
    us-gaap XBRL 태그에서 최근 연간(10-K) 값 추출.
    years=1: 최신 1개, years=3: 최근 3개 평균 반환.
    """
    try:
        entries = (
            facts.get('facts', {})
                 .get('us-gaap', {})
                 .get(tag, {})
                 .get('units', {})
                 .get(unit, [])
        )
        # 10-K 연간 보고서만 필터링 (form='10-K', fp='FY')
        annual = [
            e for e in entries
            if e.get('form') == '10-K' and e.get('fp') == 'FY'
               and e.get('val') is not None
        ]
        if not annual:
            # 10-K이지만 fp 없는 경우 fallback
            annual = [e for e in entries if e.get('form') == '10-K'
                      and e.get('val') is not None]
        if not annual:
            return None

        # 최신 순 정렬 (end 날짜 기준)
        annual_sorted = sorted(annual, key=lambda e: e.get('end', ''), reverse=True)

        # 중복 회계연도 제거 (같은 end 날짜 중 최신 접수분 유지)
        seen_end: set = set()
        dedup = []
        for e in annual_sorted:
            end_key = e.get('end', '')
            if end_key not in seen_end:
                seen_end.add(end_key)
                dedup.append(e)

        if years == 1:
            return float(dedup[0]['val'])
        else:
            vals = [float(e['val']) for e in dedup[:years]]
            return sum(vals) / len(vals) if vals else None
    except Exception as e:
        print(f'[EDGAR] extract_annual_value({tag}) 오류: {e}')
        return None


# ── Finnhub 업종 조회 ────────────────────────────────────────────
def get_finnhub_industry(ticker: str, api_key: str) -> str | None:
    """Finnhub /stock/profile2 → finnhubIndustry 문자열"""
    try:
        r = requests.get(
            f'https://finnhub.io/api/v1/stock/profile2?symbol={ticker}&token={api_key}',
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=8
        )
        if r.ok:
            return r.json().get('finnhubIndustry')
    except Exception as e:
        print(f'[Finnhub] get_industry({ticker}) 오류: {e}')
    return None


# ── 핵심 펀더멘탈 계산 ───────────────────────────────────────────
def get_edgar_fundamentals(ticker: str, finnhub_api_key: str = '',
                            current_price: float | None = None,
                            shares_outstanding: float | None = None,
                            market_cap: float | None = None) -> dict:
    """
    EDGAR XBRL + Finnhub profile → 퀄리티/밸류/DCF 계산용 dict 반환.

    Parameters
    ----------
    ticker              : 종목 티커 (예: 'AAPL')
    finnhub_api_key     : Finnhub API key (업종 조회용)
    current_price       : 현재가 (Finnhub quote에서 전달 권장)
    shares_outstanding  : 발행주식수 (단위: 천주 or 주, EDGAR로 대체 가능)
    market_cap          : 시가총액 (단위: $M, Finnhub metric에서 전달 권장)

    Returns
    -------
    dict with keys: roic, ev_ebitda, dcf_value, industry,
                    wacc_used, ebit, ebitda, debt, cash, equity,
                    current_price, error (optional)
    """
    result: dict = {}

    # 1. CIK 조회
    cik = get_cik(ticker)
    if not cik:
        return {'error': f'CIK not found for {ticker}'}

    # 2. companyfacts 로드
    facts = get_company_facts(cik)
    if not facts:
        return {'error': f'EDGAR facts unavailable for {ticker}'}

    # 3. XBRL 태그 추출 (단위: USD, 달러)
    def xv(tag, unit='USD', years=1):
        return extract_annual_value(facts, tag, unit, years)

    # 영업이익 (EBIT)
    ebit = xv('OperatingIncomeLoss')

    # 감가상각 (DA) — 우선 CashAndCash... 방식, 없으면 IncomeStatement 경로
    da = (xv('DepreciationDepletionAndAmortization') or
          xv('DepreciationAndAmortization') or
          xv('Depreciation'))

    # EBITDA
    ebitda = None
    if ebit is not None and da is not None:
        ebitda = ebit + da

    # 부채 (장기부채 + 단기부채)
    ltd  = xv('LongTermDebt') or xv('LongTermDebtNoncurrent') or 0
    std  = xv('ShortTermBorrowings') or xv('DebtCurrent') or 0
    debt = (ltd or 0) + (std or 0)

    # 현금
    cash = (xv('CashAndCashEquivalentsAtCarryingValue') or
            xv('CashCashEquivalentsAndShortTermInvestments') or 0)

    # 자기자본
    equity = (xv('StockholdersEquity') or
              xv('StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'))

    # 이자비용
    interest = xv('InterestExpense') or xv('InterestAndDebtExpense') or 0

    # 자본적지출 (CapEx)
    capex = (xv('PaymentsToAcquirePropertyPlantAndEquipment') or
             xv('CapitalExpendituresIncurringObligation') or 0)

    # 세금 관련
    tax_expense  = xv('IncomeTaxExpenseBenefit') or 0
    pretax_income = xv('IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest')

    # 4. 세율 계산
    tax_rate = None
    if pretax_income and pretax_income > 0 and tax_expense is not None:
        tax_rate = min(tax_expense / pretax_income, 0.40)  # 최대 40% 캡
    if tax_rate is None or tax_rate < 0:
        tax_rate = 0.21  # 미국 법정세율 fallback

    # 5. ROIC = NOPAT / Invested Capital
    roic = None
    if ebit is not None:
        nopat = ebit * (1 - tax_rate)
        invested_capital = (equity or 0) + (debt or 0) - (cash or 0)
        if invested_capital and invested_capital > 0:
            roic = nopat / invested_capital * 100  # %

    # 6. EV/EBITDA
    ev = None
    ev_ebitda = None
    if market_cap is not None:
        # market_cap 단위: $M (Finnhub mcap 필드)
        ev = market_cap + (debt or 0) / 1e6 - (cash or 0) / 1e6
        if ebitda and ebitda > 0:
            ebitda_m = ebitda / 1e6  # 달러 → 백만달러
            ev_ebitda = ev / ebitda_m if ebitda_m != 0 else None

    # 7. DCF 보조검증 (단순 1단계 DCF)
    dcf_value = None
    if (ebitda is not None and capex is not None and
            da is not None and tax_rate is not None and
            shares_outstanding is not None and shares_outstanding > 0):
        # FCF ≈ EBITDA - CapEx - Tax on EBIT
        fcf = (ebitda or 0) - (capex or 0) - (ebit or 0) * tax_rate if ebit else None
        if fcf and fcf > 0:
            # WACC 임시값 (Damodaran 업종 매핑 전) 10%
            _wacc_approx = 0.09
            _g = 0.025  # 영구성장률 2.5%
            # Gordon Growth: DCF ≈ FCF / (WACC - g)
            terminal = fcf / (_wacc_approx - _g)
            dcf_total = terminal  # 단순화 (growth phase 생략)
            dcf_value = dcf_total / shares_outstanding  # per share

    # 8. 업종 정보 (Damodaran 매핑용)
    industry = None
    if finnhub_api_key:
        industry = get_finnhub_industry(ticker, finnhub_api_key)

    # 결과 반환
    result = {
        # 원시 데이터
        'ebit':            ebit,
        'da':              da,
        'ebitda':          ebitda,
        'debt':            debt,
        'cash':            cash,
        'equity':          equity,
        'capex':           capex,
        'tax_expense':     tax_expense,
        'pretax_income':   pretax_income,
        'interest':        interest,
        'tax_rate':        tax_rate,
        # 계산값
        'roic':            roic,
        'ev':              ev,
        'ev_ebitda':       ev_ebitda,
        'dcf_value':       dcf_value,
        'current_price':   current_price,
        # 업종
        'industry':        industry,
        # 출처 표시용
        'data_source':     'SEC EDGAR XBRL',
    }
    return result
