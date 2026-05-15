"""
utils/edgar.py — SEC EDGAR XBRL 재무 데이터 추출 모듈
────────────────────────────────────────────────────────
Python 3.9+ 호환 (Optional[X] 사용, str | None 제거)
"""

import requests
from typing import Optional

EDGAR_BASE    = 'https://data.sec.gov'
EDGAR_HEADERS = {
    'User-Agent': 'FrankNewsDashboard daybridge2025@gmail.com',
    'Accept-Encoding': 'gzip, deflate',
}


# ── CIK 조회 ────────────────────────────────────────────────────
_cik_cache = {}

def get_cik(ticker: str) -> Optional[str]:
    """티커 → CIK(10자리). EDGAR company_tickers.json 사용."""
    ticker = ticker.upper().strip()
    if ticker in _cik_cache:
        return _cik_cache[ticker]
    try:
        r = requests.get(
            f'{EDGAR_BASE}/files/company_tickers.json',
            headers=EDGAR_HEADERS, timeout=15
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
def get_company_facts(cik: str) -> Optional[dict]:
    """SEC EDGAR companyfacts API → 전체 XBRL 재무 데이터."""
    try:
        r = requests.get(
            f'{EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik}.json',
            headers=EDGAR_HEADERS, timeout=30
        )
        if r.ok:
            return r.json()
        print(f'[EDGAR] companyfacts HTTP {r.status_code}')
    except Exception as e:
        print(f'[EDGAR] get_company_facts({cik}) 오류: {e}')
    return None


# ── XBRL 태그에서 최신 연간값 추출 ──────────────────────────────
def extract_annual_value(facts: dict, tag: str, unit: str = 'USD',
                          years: int = 1) -> Optional[float]:
    """us-gaap XBRL 태그에서 최근 연간(10-K) 값 추출."""
    try:
        entries = (
            facts.get('facts', {})
                 .get('us-gaap', {})
                 .get(tag, {})
                 .get('units', {})
                 .get(unit, [])
        )
        annual = [
            e for e in entries
            if e.get('form') == '10-K' and e.get('fp') == 'FY'
               and e.get('val') is not None
        ]
        if not annual:
            annual = [e for e in entries
                      if e.get('form') == '10-K' and e.get('val') is not None]
        if not annual:
            return None

        annual_sorted = sorted(annual, key=lambda e: e.get('end', ''), reverse=True)

        seen_end = set()
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
def get_finnhub_industry(ticker: str, api_key: str) -> Optional[str]:
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
def get_edgar_fundamentals(ticker: str,
                            finnhub_api_key: str = '',
                            current_price: Optional[float] = None,
                            shares_outstanding: Optional[float] = None,
                            market_cap: Optional[float] = None) -> dict:
    """
    EDGAR XBRL + Finnhub profile → 퀄리티/밸류/DCF 계산용 dict 반환.
    debug 키에 단계별 성공 여부를 기록해 UI에서 원인 진단 가능.
    """
    debug = {
        'ticker': ticker,
        'cik': None,
        'facts_loaded': False,
        'is_etf': False,
        'tags_found': {},
    }

    # 1. CIK 조회
    cik = get_cik(ticker)
    debug['cik'] = cik
    if not cik:
        return {'error': f'CIK 미발견 ({ticker}) — ETF이거나 티커 오류일 수 있음',
                'debug': debug}

    # 2. companyfacts 로드
    facts = get_company_facts(cik)
    debug['facts_loaded'] = facts is not None
    if not facts:
        return {'error': f'EDGAR 데이터 로드 실패 ({ticker}) — 네트워크/타임아웃',
                'debug': debug}

    # ETF 여부 확인 (us-gaap 없으면 ETF 또는 외국 기업)
    has_us_gaap = bool(facts.get('facts', {}).get('us-gaap'))
    debug['is_etf'] = not has_us_gaap
    if not has_us_gaap:
        return {'error': f'GAAP 재무데이터 없음 ({ticker}) — ETF 또는 외국 기업',
                'debug': debug}

    # 3. XBRL 태그 추출
    def xv(tag, unit='USD', years=1):
        val = extract_annual_value(facts, tag, unit, years)
        debug['tags_found'][tag] = val is not None
        return val

    ebit = xv('OperatingIncomeLoss')
    da = (xv('DepreciationDepletionAndAmortization') or
          xv('DepreciationAndAmortization') or
          xv('Depreciation'))
    debug['tags_found']['DA_any'] = da is not None

    ebitda = (ebit + da) if (ebit is not None and da is not None) else None

    ltd   = xv('LongTermDebt') or xv('LongTermDebtNoncurrent') or 0
    std   = xv('ShortTermBorrowings') or xv('DebtCurrent') or 0
    debt  = (ltd or 0) + (std or 0)

    cash  = (xv('CashAndCashEquivalentsAtCarryingValue') or
             xv('CashCashEquivalentsAndShortTermInvestments') or 0)

    equity = (xv('StockholdersEquity') or
              xv('StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'))

    interest = xv('InterestExpense') or xv('InterestAndDebtExpense') or 0
    capex    = (xv('PaymentsToAcquirePropertyPlantAndEquipment') or
                xv('CapitalExpendituresIncurringObligation') or 0)

    tax_expense   = xv('IncomeTaxExpenseBenefit') or 0
    pretax_income = xv('IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest')

    # 4. 세율
    tax_rate = None
    if pretax_income and pretax_income > 0 and tax_expense:
        tax_rate = min(tax_expense / pretax_income, 0.40)
    if tax_rate is None or tax_rate < 0:
        tax_rate = 0.21

    # 5. ROIC
    roic = None
    if ebit is not None:
        nopat = ebit * (1 - tax_rate)
        invested_capital = (equity or 0) + (debt or 0) - (cash or 0)
        if invested_capital and invested_capital > 0:
            roic = nopat / invested_capital * 100

    # 6. EV/EBITDA
    ev = ev_ebitda = None
    if market_cap is not None:
        ev = market_cap + (debt or 0) / 1e6 - (cash or 0) / 1e6
        if ebitda and ebitda > 0:
            ebitda_m = ebitda / 1e6
            ev_ebitda = ev / ebitda_m if ebitda_m else None

    # 7. DCF
    dcf_value = None
    if (ebitda and capex is not None and ebit is not None and
            shares_outstanding and shares_outstanding > 0):
        fcf = ebitda - (capex or 0) - ebit * tax_rate
        if fcf > 0:
            _w, _g = 0.09, 0.025
            dcf_value = (fcf / (_w - _g)) / shares_outstanding

    # 8. 업종
    industry = get_finnhub_industry(ticker, finnhub_api_key) if finnhub_api_key else None

    debug['roic_ok']     = roic is not None
    debug['ev_ebitda_ok']= ev_ebitda is not None
    debug['dcf_ok']      = dcf_value is not None

    return {
        'ebit': ebit, 'da': da, 'ebitda': ebitda,
        'debt': debt, 'cash': cash, 'equity': equity,
        'capex': capex, 'tax_expense': tax_expense,
        'pretax_income': pretax_income, 'interest': interest,
        'tax_rate': tax_rate,
        'roic': roic, 'ev': ev, 'ev_ebitda': ev_ebitda,
        'dcf_value': dcf_value, 'current_price': current_price,
        'industry': industry,
        'data_source': 'SEC EDGAR XBRL',
        'debug': debug,
    }
