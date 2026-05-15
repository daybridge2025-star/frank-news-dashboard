"""
utils/edgar.py -- SEC EDGAR XBRL financial data extraction module
Python 3.9+ compatible (uses Optional[X])
CIK lookup order: built-in table -> EDGAR EFTS search -> company_tickers.json (fallback)
"""

import requests
from typing import Optional

EDGAR_BASE    = 'https://data.sec.gov'
EDGAR_HEADERS = {
    'User-Agent': 'FrankNewsDashboard daybridge2025@gmail.com',
    'Accept-Encoding': 'gzip, deflate',
}

# Known CIK table -- avoids slow company_tickers.json download on Streamlit Cloud
# Empty string = ETF or no GAAP 10-K
KNOWN_CIKS = {
    'AAPL':  '0000320193',
    'MSFT':  '0000789019',
    'GOOGL': '0001652044',
    'GOOG':  '0001652044',
    'AMZN':  '0001018724',
    'META':  '0001326801',
    'NVDA':  '0001045810',
    'TSLA':  '0001318605',
    'PLTR':  '0001321655',
    'RKLB':  '0001819989',
    'IONQ':  '0001824920',
    'JOBY':  '0001840292',
    'KTOS':  '0001069974',
    'VST':   '0001692819',
    'ONDS':  '0001725123',
    'SATL':  '0001849821',
    # ETFs -- no 10-K GAAP
    'SOXL':  '',
    'TQQQ':  '',
    'QQQ':   '',
    'SPY':   '',
    'SOXX':  '',
    'ARKK':  '',
    'SQQQ':  '',
    'UPRO':  '',
    'UDOW':  '',
    # Foreign filers (ADR / 20-F)
    'KT':    '0001120810',
    'TSM':   '0001046179',
    'BABA':  '0001577552',
    'NIO':   '0001747571',
    # Other US stocks
    'AMD':   '0000002488',
    'INTC':  '0000050863',
    'QCOM':  '0000804328',
    'CRM':   '0001108524',
    'NFLX':  '0001065280',
    'DIS':   '0001001039',
    'BA':    '0000012927',
    'JPM':   '0000019617',
    'GS':    '0000886982',
    'V':     '0001403161',
    'MA':    '0001141391',
}

FOREIGN_FILERS = {'SATL', 'KT', 'TSM', 'BABA', 'NIO'}

_cik_cache = {}


def get_cik(ticker: str) -> Optional[str]:
    ticker = ticker.upper().strip()
    if ticker in KNOWN_CIKS:
        val = KNOWN_CIKS[ticker]
        return val if val else None
    if ticker in _cik_cache:
        return _cik_cache[ticker]
    cik = _get_cik_via_efts(ticker)
    if cik:
        _cik_cache[ticker] = cik
        return cik
    cik = _get_cik_via_bulk(ticker)
    if cik:
        _cik_cache[ticker] = cik
    return cik


def _get_cik_via_efts(ticker: str) -> Optional[str]:
    try:
        r = requests.get(
            'https://efts.sec.gov/LATEST/search-index?q=%22' + ticker + '%22&forms=10-K',
            headers=EDGAR_HEADERS, timeout=8
        )
        if not r.ok:
            return None
        hits = r.json().get('hits', {}).get('hits', [])
        for hit in hits:
            src = hit.get('_source', {})
            if src.get('ticker_symbol', '').upper() == ticker:
                entity_id = src.get('entity_id', '')
                if entity_id:
                    return str(int(entity_id)).zfill(10)
    except Exception as e:
        print('[EDGAR] EFTS lookup(' + ticker + '): ' + str(e))
    return None


def _get_cik_via_bulk(ticker: str) -> Optional[str]:
    try:
        r = requests.get(
            EDGAR_BASE + '/files/company_tickers.json',
            headers=EDGAR_HEADERS, timeout=20
        )
        if not r.ok:
            return None
        for item in r.json().values():
            if item.get('ticker', '').upper() == ticker:
                return str(item['cik_str']).zfill(10)
    except Exception as e:
        print('[EDGAR] bulk CIK lookup(' + ticker + '): ' + str(e))
    return None


def get_company_facts(cik: str) -> Optional[dict]:
    try:
        r = requests.get(
            EDGAR_BASE + '/api/xbrl/companyfacts/CIK' + cik + '.json',
            headers=EDGAR_HEADERS, timeout=30
        )
        if r.ok:
            return r.json()
        print('[EDGAR] companyfacts HTTP ' + str(r.status_code) + ' for CIK ' + cik)
    except Exception as e:
        print('[EDGAR] get_company_facts(' + cik + '): ' + str(e))
    return None


def extract_annual_values_list(facts: dict, tag: str, unit: str = 'USD',
                                n: int = 2) -> list:
    """최근 n개 연간 값을 리스트로 반환 (최신순). YoY 성장률 계산용."""
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
            if e.get('form') in ('10-K', '20-F') and e.get('fp') == 'FY'
               and e.get('val') is not None
        ]
        if not annual:
            annual = [e for e in entries
                      if e.get('form') in ('10-K', '20-F')
                      and e.get('val') is not None]
        if not annual:
            return []
        annual_sorted = sorted(annual, key=lambda e: e.get('end', ''), reverse=True)
        seen_end = set()
        dedup = []
        for e in annual_sorted:
            end_key = e.get('end', '')
            if end_key not in seen_end:
                seen_end.add(end_key)
                dedup.append(e)
        return [float(e['val']) for e in dedup[:n]]
    except Exception as e:
        print('[EDGAR] extract_annual_values_list(' + tag + '): ' + str(e))
        return []


def extract_annual_value(facts: dict, tag: str, unit: str = 'USD',
                          years: int = 1) -> Optional[float]:
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
            if e.get('form') in ('10-K', '20-F') and e.get('fp') == 'FY'
               and e.get('val') is not None
        ]
        if not annual:
            annual = [e for e in entries
                      if e.get('form') in ('10-K', '20-F')
                      and e.get('val') is not None]
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
        vals = [float(e['val']) for e in dedup[:years]]
        return sum(vals) / len(vals) if vals else None
    except Exception as e:
        print('[EDGAR] extract_annual_value(' + tag + '): ' + str(e))
        return None


def get_finnhub_industry(ticker: str, api_key: str) -> Optional[str]:
    try:
        r = requests.get(
            'https://finnhub.io/api/v1/stock/profile2?symbol=' + ticker + '&token=' + api_key,
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=8
        )
        if r.ok:
            return r.json().get('finnhubIndustry')
    except Exception as e:
        print('[Finnhub] get_industry(' + ticker + '): ' + str(e))
    return None


def get_edgar_fundamentals(ticker: str,
                            finnhub_api_key: str = '',
                            current_price: Optional[float] = None,
                            shares_outstanding: Optional[float] = None,
                            market_cap: Optional[float] = None) -> dict:
    debug = {
        'ticker': ticker,
        'cik': None,
        'facts_loaded': False,
        'is_etf': ticker.upper() in [k for k, v in KNOWN_CIKS.items() if v == ''],
        'is_foreign': ticker.upper() in FOREIGN_FILERS,
        'tags_found': {},
    }

    if debug['is_etf']:
        return {'error': 'ETF -- no GAAP financials (' + ticker + ')', 'debug': debug}

    cik = get_cik(ticker)
    debug['cik'] = cik
    if not cik:
        return {'error': 'CIK not found (' + ticker + ') -- not registered on EDGAR', 'debug': debug}

    facts = get_company_facts(cik)
    debug['facts_loaded'] = facts is not None
    if not facts:
        return {'error': 'EDGAR data load failed (CIK ' + cik + ')', 'debug': debug}

    def _tag(tag, unit='USD', years=1):
        val = extract_annual_value(facts, tag, unit, years)
        debug['tags_found'][tag] = val is not None
        return val

    # 매출: 최신 2년치 추출 (YoY 성장률 계산용)
    _rev_list = (extract_annual_values_list(facts, 'Revenues', n=2) or
                 extract_annual_values_list(facts, 'RevenueFromContractWithCustomerExcludingAssessedTax', n=2))
    revenue      = float(_rev_list[0]) if _rev_list else None
    revenue_prev = float(_rev_list[1]) if len(_rev_list) > 1 else None

    ebit         = _tag('OperatingIncomeLoss')
    gross_profit = _tag('GrossProfit')

    da = (_tag('DepreciationDepletionAndAmortization') or
          _tag('DepreciationAndAmortization') or
          _tag('Depreciation'))

    ebitda = None
    if ebit is not None and da is not None:
        ebitda = ebit + da
    elif ebit is not None:
        ebitda = ebit

    total_assets = _tag('Assets')
    total_equity = (_tag('StockholdersEquity') or
                    _tag('StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'))
    debt_lt      = _tag('LongTermDebt')
    debt_lt2     = _tag('LongTermDebtNoncurrent')
    debt         = debt_lt or debt_lt2 or 0
    cash         = (_tag('CashAndCashEquivalentsAtCarryingValue') or
                    _tag('CashCashEquivalentsAndShortTermInvestments') or 0)

    cfo       = _tag('NetCashProvidedByUsedInOperatingActivities')
    capex_raw = _tag('PaymentsToAcquirePropertyPlantAndEquipment')
    capex     = abs(capex_raw) if capex_raw is not None else None

    net_income    = _tag('NetIncomeLoss')
    tax_expense   = _tag('IncomeTaxExpense') or _tag('IncomeTaxExpenseBenefit')
    pretax_income = _tag('IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest')

    tax_rate = 0.21
    if tax_expense and pretax_income and pretax_income != 0:
        tax_rate = max(0.0, min(0.5, tax_expense / pretax_income))

    roic = None
    nopat = None
    invested_capital = None
    if ebit is not None:
        nopat = ebit * (1 - tax_rate)
        if total_equity and total_equity > 0:
            invested_capital = total_equity + (debt or 0) - (cash or 0)
            if invested_capital > 0:
                roic = round((nopat / invested_capital) * 100, 2)

    ev = None
    ev_ebitda = None
    if market_cap is not None and market_cap > 0:
        ev = market_cap + (debt or 0) / 1e6 - (cash or 0) / 1e6
        if ebitda and ebitda > 0:
            ev_ebitda = round(ev / (ebitda / 1e6), 1)

    gross_margin = None
    if gross_profit and revenue and revenue > 0:
        gross_margin = round(gross_profit / revenue * 100, 1)

    op_margin = None
    if ebit is not None and revenue and revenue > 0:
        op_margin = round(ebit / revenue * 100, 1)

    fcf = None
    if cfo is not None and capex is not None:
        fcf = cfo - capex

    dcf_value = None
    if fcf and fcf > 0 and shares_outstanding and shares_outstanding > 0:
        wacc_est = 0.10
        g = 0.025
        if wacc_est > g:
            terminal  = fcf / (wacc_est - g)
            dcf_value = terminal / shares_outstanding

    industry = None
    if finnhub_api_key:
        industry = get_finnhub_industry(ticker, finnhub_api_key)

    return {
        'ticker':             ticker,
        'cik':                cik,
        'industry':           industry,
        'revenue':            revenue,
        'revenue_prev':       revenue_prev,
        'gross_profit':       gross_profit,
        'gross_margin':       gross_margin,
        'ebit':               ebit,
        'op_margin':          op_margin,
        'ebitda':             ebitda,
        'da':                 da,
        'net_income':         net_income,
        'total_assets':       total_assets,
        'equity':             total_equity,
        'debt':               debt,
        'cash':               cash,
        'cfo':                cfo,
        'capex':              capex,
        'fcf':                fcf,
        'nopat':              nopat,
        'invested_capital':   invested_capital,
        'roic':               roic,
        'ev':                 ev,
        'ev_ebitda':          ev_ebitda,
        'tax_rate':           tax_rate,
        'current_price':      current_price,
        'shares_outstanding': shares_outstanding,
        'market_cap':         market_cap,
        'dcf_value':          dcf_value,
        'debug':              debug,
    }
