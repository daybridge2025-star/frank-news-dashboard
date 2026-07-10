"""
KRX Open API 클라이언트 (openapi.krx.co.kr — AUTH_KEY 발급 필요)

실측 확인 (2026-07-10, 유효 키 + 서비스 승인 상태):
- BASE_URL/PATHS/필드명 전부 실제 200 응답으로 검증됨.
- 응답은 {"OutBlock_1": [ {행}, ... ]} 구조.
- 지수 엔드포인트: 해당일 전체 지수(예: KOSPI 51개)를 반환 → 헤드라인은 IDX_NM으로 필터.
- 종목 엔드포인트: 해당일 전체 종목(KOSPI ~945개)을 반환. isuCd 파라미터는 무시되므로
  한 번 받아 로컬에서 ISU_CD로 필터하는 게 정석(종목별 호출은 매번 전체를 받아 비효율).
- 데이터는 T+1: 당일 장마감 직후엔 당일치가 없고 전 영업일이 최신인 경우가 많다.
- 투자자별 순매수(외국인/기관/개인/연기금)는 이 카탈로그에 없음 → pykrx(utils/krx_scrape) 사용.

주요 필드:
  지수  : BAS_DD, IDX_CLSS, IDX_NM, CLSPRC_IDX(종가), CMPPREVDD_IDX(전일대비),
          FLUC_RT(등락률%), OPNPRC_IDX, HGPRC_IDX, LWPRC_IDX, ACC_TRDVOL, ACC_TRDVAL, MKTCAP
  종목  : BAS_DD, ISU_CD, ISU_NM, MKT_NM, TDD_CLSPRC(종가), CMPPREVDD_PRC(전일대비),
          FLUC_RT(등락률%), TDD_OPNPRC, TDD_HGPRC, TDD_LWPRC, ACC_TRDVOL, ACC_TRDVAL, MKTCAP
"""

import os
import requests

BASE_URL = 'http://data-dbg.krx.co.kr/svc/apis'

PATHS = {
    'kospi_index':  '/idx/kospi_dd_trd',   # KOSPI 시리즈 일별시세
    'kosdaq_index': '/idx/kosdaq_dd_trd',  # 코스닥 시리즈 일별시세
    'stock_kospi':  '/sto/stk_bydd_trd',   # 유가증권 개별종목 일별매매정보 (전종목)
    'stock_kosdaq': '/sto/ksq_bydd_trd',   # 코스닥 개별종목 일별매매정보 (전종목)
}


def _get_auth_key():
    key = os.environ.get('KRX_AUTH_KEY', '')
    if not key:
        print('[KRX] KRX_AUTH_KEY 없음')
    return key


def _call(path, params, auth_key=None, timeout=30):
    """공통 호출 헬퍼. 실패해도 빈 리스트 반환 — 한 항목 실패가 전체 수집을 막지 않게 한다."""
    auth_key = auth_key or _get_auth_key()
    if not auth_key:
        return []
    try:
        resp = requests.get(
            BASE_URL + path,
            params=params,
            headers={'AUTH_KEY': auth_key},
            timeout=timeout,
        )
        if resp.status_code != 200:
            print(f'[KRX] {path} 오류: HTTP {resp.status_code} — {resp.text[:200]}')
            return []
        data = resp.json()
        # 최상위 키는 OutBlock_1 — 리스트인 첫 값을 그대로 반환(키명 변경에도 견고)
        for v in data.values():
            if isinstance(v, list):
                return v
        return []
    except Exception as e:
        print(f'[KRX] {path} 호출 실패: {e}')
        return []


def get_kospi_index(bas_dd, auth_key=None):
    """KOSPI 지수 일별시세(전 지수). bas_dd: 'YYYYMMDD'"""
    return _call(PATHS['kospi_index'], {'basDd': bas_dd}, auth_key)


def get_kosdaq_index(bas_dd, auth_key=None):
    """코스닥 지수 일별시세(전 지수)."""
    return _call(PATHS['kosdaq_index'], {'basDd': bas_dd}, auth_key)


def get_kospi_stocks(bas_dd, auth_key=None):
    """유가증권 전 종목 일별매매정보(1회 호출, 로컬 필터용). 응답이 커서 timeout 넉넉히."""
    return _call(PATHS['stock_kospi'], {'basDd': bas_dd}, auth_key, timeout=60)


def get_kosdaq_stocks(bas_dd, auth_key=None):
    """코스닥 전 종목 일별매매정보(1회 호출)."""
    return _call(PATHS['stock_kosdaq'], {'basDd': bas_dd}, auth_key, timeout=60)


def headline_index(rows, name):
    """지수 전체 리스트에서 헤드라인 한 줄 추출(예: '코스피', '코스닥')."""
    for r in rows or []:
        if str(r.get('IDX_NM', '')).strip() == name:
            return r
    return None


def stocks_by_code(rows):
    """종목 전체 리스트를 ISU_CD → 행 dict로 변환(로컬 필터용)."""
    return {str(r.get('ISU_CD', '')).strip(): r for r in (rows or [])}
