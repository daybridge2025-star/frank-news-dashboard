"""
utils/damodaran.py — Damodaran 업종 벤치마크 조회 및 WACC 재레버링 모듈
──────────────────────────────────────────────────────────────────────
데이터 출처: Aswath Damodaran (NYU Stern), Jan 2025
가정: Rf = 3.95%, ERP = 4.46%

주요 함수:
  load_tables()                         → CSV 4개 로드 (캐시)
  match_industry(finnhub_industry)      → Damodaran 업종명 퍼지 매칭
  get_industry_wacc(industry)           → 업종 WACC (%)
  get_industry_ev_ebitda(industry)      → 업종 EV/EBITDA 중앙값
  get_industry_roic(industry)           → 업종 ROIC (%)
  relever_wacc(industry, de_ratio, tax_rate) → 기업 맞춤 WACC 재계산
  enrich_fundamentals(fundamentals)     → EDGAR dict에 Damodaran 벤치마크 추가
"""

import os
import csv
import difflib
from functools import lru_cache
from typing import Optional

# ── CSV 경로 설정 ────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA = os.path.join(_HERE, 'data')

_WACC_CSV     = os.path.join(_DATA, 'damodaran_wacc.csv')
_EV_CSV       = os.path.join(_DATA, 'damodaran_ev_ebitda.csv')
_EVA_CSV      = os.path.join(_DATA, 'damodaran_eva.csv')
_BETA_CSV     = os.path.join(_DATA, 'damodaran_beta.csv')

# 가정 상수
RF  = 3.95   # 무위험이자율 (%)
ERP = 4.46   # 주식위험프리미엄 (%)


# ── CSV 로더 ─────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def load_tables() -> dict:
    """4개 CSV를 딕셔너리(industry → row dict)로 로드. LRU 캐시로 1회만 읽음."""
    def _load(path):
        tbl = {}
        if not os.path.exists(path):
            return tbl
        with open(path, encoding='utf-8', newline='') as f:
            for row in csv.DictReader(f):
                ind = row.get('industry', '').strip()
                if ind:
                    tbl[ind] = row
        return tbl

    return {
        'wacc':     _load(_WACC_CSV),
        'ev':       _load(_EV_CSV),
        'eva':      _load(_EVA_CSV),
        'beta':     _load(_BETA_CSV),
    }


# ── Finnhub → Damodaran 업종 퍼지 매핑 ──────────────────────────
# Finnhub finnhubIndustry 값 → Damodaran 업종명 우선 매핑 테이블
_INDUSTRY_MAP: dict[str, str] = {
    # Technology
    'Technology':                      'Software (System & Application)',
    'Software':                        'Software (System & Application)',
    'Software-Infrastructure':         'Software (System & Application)',
    'Software-Application':            'Software (System & Application)',
    'Internet Software/Services':      'Software (Internet)',
    'Internet':                        'Software (Internet)',
    'Electronic Gaming & Multimedia':  'Software (Entertainment)',
    'Semiconductors':                  'Semiconductor',
    'Semiconductor Equipment & Materials': 'Semiconductor Equip',
    'Computer Hardware':               'Computers/Peripherals',
    'Consumer Electronics':            'Electronics (Consumer & Office)',
    'Electronic Components':           'Electronics (General)',
    'Information Technology Services': 'Computer Services',

    # Communication
    'Communication Services':          'Telecom. Services',
    'Telecom Services':                'Telecom. Services',
    'Wireless':                        'Telecom (Wireless)',
    'Broadcasting':                    'Broadcasting',
    'Entertainment':                   'Entertainment',
    'Interactive Media & Services':    'Software (Internet)',
    'Publishing':                      'Publishing & Newspapers',

    # Healthcare
    'Healthcare':                      'Healthcare Products',
    'Biotechnology':                   'Drugs (Biotechnology)',
    'Drug Manufacturers':              'Drugs (Pharmaceutical)',
    'Medical Devices':                 'Healthcare Products',
    'Medical Instruments & Supplies':  'Healthcare Products',
    'Health Information Services':     'Heathcare Information and Technology',
    'Diagnostics & Research':          'Healthcare Support Services',
    'Healthcare Plans':                'Healthcare Support Services',
    'Hospitals':                       'Hospitals/Healthcare Facilities',
    'Medical Care Facilities':         'Hospitals/Healthcare Facilities',

    # Energy
    'Energy':                          'Oil/Gas (Production and Exploration)',
    'Oil & Gas E&P':                   'Oil/Gas (Production and Exploration)',
    'Oil & Gas Integrated':            'Oil/Gas (Integrated)',
    'Oil & Gas Equipment & Services':  'Oilfield Svcs/Equip.',
    'Oil & Gas Midstream':             'Oil/Gas Distribution',
    'Oil & Gas Refining & Marketing':  'Oil/Gas Distribution',
    'Coal':                            'Coal & Related Energy',
    'Utilities':                       'Utility (General)',
    'Utilities-Electric':              'Utility (General)',
    'Utilities-Regulated Electric':    'Power',
    'Utilities-Water':                 'Utility (Water)',
    'Renewable Utilities':             'Green & Renewable Energy',

    # Financials
    'Financial Services':              'Financial Svcs. (Non-bank & Insurance)',
    'Banks':                           'Banks (Regional)',
    'Banks-Regional':                  'Banks (Regional)',
    'Banks-Diversified':               'Bank (Money Center)',
    'Insurance':                       'Insurance (General)',
    'Insurance-Life':                  'Insurance (Life)',
    'Insurance-Property & Casualty':   'Insurance (Prop/Cas.)',
    'Insurance-Reinsurance':           'Reinsurance',
    'Asset Management':                'Investments & Asset Management',
    'Capital Markets':                 'Brokerage & Investment Banking',
    'REITs':                           'R.E.I.T.',
    'Real Estate':                     'Real Estate (General/Diversified)',
    'Real Estate-Diversified':         'Real Estate (General/Diversified)',
    'Real Estate Development':         'Real Estate (Development)',
    'Real Estate Services':            'Real Estate (Operations & Services)',

    # Consumer
    'Consumer Defensive':              'Food Processing',
    'Food Distribution':               'Food Wholesalers',
    'Beverages-Non-Alcoholic':         'Beverage (Soft)',
    'Beverages-Alcoholic':             'Beverage (Alcoholic)',
    'Tobacco':                         'Tobacco',
    'Household & Personal Products':   'Household Products',
    'Apparel & Fashion':               'Apparel',
    'Footwear & Accessories':          'Shoe',
    'Specialty Retail':                'Retail (Special Lines)',
    'Consumer Discretionary':          'Retail (General)',
    'Restaurants':                     'Restaurant/Dining',
    'Leisure':                         'Recreation',
    'Lodging':                         'Hotel/Gaming',
    'Casinos & Gaming':                'Hotel/Gaming',

    # Industrials
    'Industrials':                     'Machinery',
    'Aerospace & Defense':             'Aerospace/Defense',
    'Airlines':                        'Air Transport',
    'Railroads':                       'Transportation (Railroads)',
    'Trucking':                        'Trucking',
    'Transportation & Logistics':      'Transportation',
    'Marine Shipping':                 'Shipbuilding & Marine',
    'Engineering & Construction':      'Engineering/Construction',
    'Building Products & Equipment':   'Building Materials',
    'Specialty Chemicals':             'Chemical (Specialty)',
    'Chemicals':                       'Chemical (Diversified)',
    'Steel':                           'Steel',
    'Metals & Mining':                 'Metals & Mining',
    'Paper & Forest Products':         'Paper/Forest Products',
    'Packaging & Containers':          'Packaging & Container',
    'Auto Manufacturers':              'Auto & Truck',
    'Auto Parts':                      'Auto Parts',
    'Farm & Construction Equipment':   'Machinery',
    'Agriculture':                     'Farming/Agriculture',
    'Electrical Equipment & Parts':    'Electrical Equipment',
    'Waste Management':                'Environmental & Waste Services',
    'Defense':                         'Aerospace/Defense',
}

# ── 티커별 업종 강제 오버라이드 (Finnhub 오분류 수정) ─────────────
INDUSTRY_OVERRIDE: dict[str, str] = {
    'TSLA':  'Electrical Equipment',
    'RKLB':  'Aerospace/Defense',
    'IONQ':  'Electronics (General)',
    'JOBY':  'Aerospace/Defense',
    'PLTR':  'Software (System & Application)',
    'KTOS':  'Aerospace/Defense',
    'ONDS':  'Telecom. Services',
    'SATL':  'Telecom. Services',
    'VST':   'Power',
    'NIO':   'Auto & Truck',
}

# ── 티커별 UI 업종 선택지 (label, damodaran_key) ──────────────────
INDUSTRY_CANDIDATES: dict[str, list] = {
    'TSLA': [
        ('Electrical Equipment',           'Electrical Equipment'),
        ('Auto & Truck',                   'Auto & Truck'),
        ('Software (System & Application)','Software (System & Application)'),
        ('Green & Renewable Energy',       'Green & Renewable Energy'),
    ],
    'RKLB': [
        ('Aerospace/Defense',              'Aerospace/Defense'),
        ('Electronics (General)',          'Electronics (General)'),
    ],
    'IONQ': [
        ('Electronics (General)',          'Electronics (General)'),
        ('Semiconductor',                  'Semiconductor'),
        ('Software (System & Application)','Software (System & Application)'),
    ],
    'JOBY': [
        ('Aerospace/Defense',              'Aerospace/Defense'),
        ('Air Transport',                  'Air Transport'),
    ],
    'PLTR': [
        ('Software (System & Application)','Software (System & Application)'),
        ('Software (Internet)',            'Software (Internet)'),
        ('Computer Services',             'Computer Services'),
    ],
    'KTOS': [
        ('Aerospace/Defense',              'Aerospace/Defense'),
        ('Electronics (General)',          'Electronics (General)'),
    ],
    'ONDS': [
        ('Telecom. Services',              'Telecom. Services'),
        ('Telecom (Wireless)',             'Telecom (Wireless)'),
        ('Electronics (General)',          'Electronics (General)'),
    ],
    'SATL': [
        ('Telecom. Services',              'Telecom. Services'),
        ('Telecom (Wireless)',             'Telecom (Wireless)'),
        ('Aerospace/Defense',              'Aerospace/Defense'),
    ],
    'VST': [
        ('Power',                          'Power'),
        ('Utility (General)',              'Utility (General)'),
        ('Green & Renewable Energy',       'Green & Renewable Energy'),
    ],
    'NIO': [
        ('Auto & Truck',                   'Auto & Truck'),
        ('Electrical Equipment',           'Electrical Equipment'),
    ],
}


@lru_cache(maxsize=256)
def match_industry(finnhub_industry: str) -> Optional[str]:
    """
    Finnhub finnhubIndustry → Damodaran 업종명 변환.
    1) 직접 매핑 테이블 우선
    2) difflib 퍼지 매칭 fallback (유사도 0.5 이상)
    """
    if not finnhub_industry:
        return None

    # 1. 직접 매핑
    direct = _INDUSTRY_MAP.get(finnhub_industry)
    if direct:
        return direct

    # 2. 퍼지 매핑 — Damodaran 업종명 목록 대상
    tables = load_tables()
    damod_industries = list(tables['wacc'].keys())
    matches = difflib.get_close_matches(
        finnhub_industry, damod_industries, n=1, cutoff=0.5
    )
    if matches:
        return matches[0]

    # 3. 키워드 부분 일치
    fi_lower = finnhub_industry.lower()
    for damod in damod_industries:
        if any(kw in fi_lower for kw in damod.lower().split()[:2]):
            return damod

    return None


# ── 업종별 벤치마크 조회 ─────────────────────────────────────────
def _flt(v) -> Optional[float]:
    if v is None or v == '': return None
    try: return float(v)
    except: return None


def get_industry_wacc(industry: str) -> Optional[float]:
    """업종 WACC (%). 없으면 None."""
    tables = load_tables()
    row = tables['wacc'].get(industry)
    return _flt(row['wacc']) if row else None


def get_industry_ev_ebitda(industry: str) -> Optional[float]:
    """업종 EV/EBITDA (흑자기업 기준 중앙값). 없으면 None."""
    tables = load_tables()
    row = tables['ev'].get(industry)
    return _flt(row['ev_ebitda_pos']) if row else None


def get_industry_roic(industry: str) -> Optional[float]:
    """업종 ROIC (%). 없으면 None."""
    tables = load_tables()
    row = tables['eva'].get(industry)
    return _flt(row['roic']) if row else None


def get_industry_beta_unlevered(industry: str) -> Optional[float]:
    """업종 언레버드 베타 (현금조정). 없으면 None."""
    tables = load_tables()
    row = tables['beta'].get(industry)
    return _flt(row['beta_unlev_cashadj']) if row else None


# ── WACC 재레버링 ────────────────────────────────────────────────
def relever_wacc(industry: str,
                 de_ratio: float,          # 기업 D/E 비율 (%)
                 tax_rate: float,          # 기업 실효세율 (0~1)
                 pretax_cod: Optional[float] = None  # 세전 부채비용 (%), 없으면 업종값
                 ) -> Optional[float]:
    """
    다모다란 언레버드 베타로 기업 맞춤 WACC 재계산.

    β_L = β_U × [1 + (1-t) × D/E]
    CoE = Rf + β_L × ERP
    WACC = CoE × E/(D+E) + CoD_AT × D/(D+E)
    """
    beta_u = get_industry_beta_unlevered(industry)
    if beta_u is None:
        return None

    de = de_ratio / 100  # % → 소수
    t  = tax_rate

    # 레버드 베타 재계산
    beta_l = beta_u * (1 + (1 - t) * de)

    # 자기자본비용 (CoE)
    coe = RF + beta_l * ERP  # %

    # 세후 부채비용 (CoD_AT)
    if pretax_cod is None:
        tables = load_tables()
        row = tables['wacc'].get(industry)
        pretax_cod = _flt(row['pretax_cod']) if row else 5.0
    cod_at = (pretax_cod or 5.0) * (1 - t)

    # D/(D+E), E/(D+E)
    debt_pct   = de / (1 + de)
    equity_pct = 1 / (1 + de)

    wacc = coe * equity_pct + cod_at * debt_pct
    return round(wacc, 2)


# -- Main: EDGAR dict + Damodaran benchmark --
def enrich_fundamentals(fundamentals: dict, industry_override: str = '') -> dict:
    f = fundamentals.copy()
    ticker = f.get('ticker', '').upper()

    # Industry priority: user override > auto override > finnhub
    if industry_override:
        damod_ind = industry_override
        industry_source = 'override_user'
    elif ticker in INDUSTRY_OVERRIDE:
        damod_ind = INDUSTRY_OVERRIDE[ticker]
        industry_source = 'override_auto'
    else:
        finnhub_ind = f.get('industry')
        damod_ind = match_industry(finnhub_ind) if finnhub_ind else None
        industry_source = 'finnhub_auto'

    f['damod_industry'] = damod_ind
    f['industry_source'] = industry_source

    if not damod_ind:
        for k in ['industry_wacc', 'industry_ev_ebitda', 'industry_roic',
                  'wacc_used', 'roic_wacc_spread', 'revenue_growth_yoy',
                  'psr', 'fcf_margin', 'rule_of_40', 'rdcf_implied_g']:
            f[k] = None
        f['is_high_growth'] = False
        return f

    ind_wacc      = get_industry_wacc(damod_ind)
    ind_ev_ebitda = get_industry_ev_ebitda(damod_ind)
    ind_roic      = get_industry_roic(damod_ind)
    ind_beta      = get_industry_beta_unlevered(damod_ind)

    f['industry_wacc']      = ind_wacc
    f['industry_ev_ebitda'] = ind_ev_ebitda
    f['industry_roic']      = ind_roic
    f['industry_beta']      = ind_beta

    debt   = f.get('debt', 0) or 0
    equity = f.get('equity') or 0
    tax_r  = f.get('tax_rate', 0.21) or 0.21

    if equity > 0 and debt >= 0:
        de_ratio       = (debt / equity) * 100
        wacc_relevered = relever_wacc(damod_ind, de_ratio, tax_r)
        wacc_used      = wacc_relevered if wacc_relevered else ind_wacc
    else:
        wacc_used = ind_wacc

    f['wacc_used'] = wacc_used

    roic = f.get('roic')
    if roic is not None and wacc_used is not None:
        f['roic_wacc_spread'] = round(roic - wacc_used, 2)
    else:
        f['roic_wacc_spread'] = None

    # High-growth metrics
    revenue      = f.get('revenue')
    revenue_prev = f.get('revenue_prev')
    fcf          = f.get('fcf')
    ev           = f.get('ev')
    net_income   = f.get('net_income')

    rev_growth = None
    if revenue and revenue_prev and revenue_prev != 0:
        rev_growth = round((revenue - revenue_prev) / abs(revenue_prev) * 100, 1)
    f['revenue_growth_yoy'] = rev_growth

    psr = None
    if ev and revenue and revenue > 0:
        psr = round(ev / (revenue / 1e6), 1)
    f['psr'] = psr

    fcf_margin = None
    if fcf is not None and revenue and revenue > 0:
        fcf_margin = round(fcf / revenue * 100, 1)
    f['fcf_margin'] = fcf_margin

    r40 = None
    if rev_growth is not None and fcf_margin is not None:
        r40 = round(rev_growth + fcf_margin, 1)
    f['rule_of_40'] = r40

    is_hg = False
    if rev_growth is not None and rev_growth > 20:
        is_hg = True
    if psr is not None and psr > 8:
        is_hg = True
    if net_income is not None and net_income < 0:
        is_hg = True
    f['is_high_growth'] = is_hg

    # ── 복합 기업 플래그 (업종 후보 3개 이상) ──────────────────────
    is_conglom = len(INDUSTRY_CANDIDATES.get(ticker, [])) >= 3
    f['is_conglomerate'] = is_conglom

    # ── Scenario DCF (Bear / Base / Bull) ────────────────────────
    shares = f.get('shares_outstanding')
    if fcf is not None and fcf > 0 and wacc_used and shares and shares > 0:
        w = wacc_used / 100
        # Bear: WACC+2%, terminal g=1.5%
        bear_d = (w + 0.02) - 0.015
        # Base: current WACC, terminal g=2.5%
        base_d = w - 0.025
        # Bull: WACC-1%, FCF×1.5, terminal g=4%
        bull_d = (w - 0.01) - 0.040
        f['bear_dcf'] = round((fcf / bear_d) / shares, 2) if bear_d > 0 else None
        f['base_dcf'] = round((fcf / base_d) / shares, 2) if base_d > 0 else None
        f['bull_dcf'] = round((fcf * 1.5 / bull_d) / shares, 2) if bull_d > 0 else None
    else:
        f['bear_dcf'] = None
        f['base_dcf'] = None
        f['bull_dcf'] = None

    rdcf_g = None
    if wacc_used and ev and ev > 0 and fcf is not None:
        rdcf_g = round(((wacc_used / 100) - (fcf / 1e6 / ev)) * 100, 2)
    f['rdcf_implied_g'] = rdcf_g

    # DCF recalc with wacc_used
    if (wacc_used and f.get('ebitda') and f.get('capex') is not None
            and f.get('ebit') and f.get('shares_outstanding')):
        fcf_dcf = f['ebitda'] - f['capex'] - f['ebit'] * tax_r
        if fcf_dcf > 0:
            _wacc = wacc_used / 100
            _g    = 0.025
            if _wacc > _g:
                f['dcf_value'] = (fcf_dcf / (_wacc - _g)) / f['shares_outstanding']

    return f
