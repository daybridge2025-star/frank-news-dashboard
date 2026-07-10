"""
KRX Open API 클라이언트 (openapi.krx.co.kr — AUTH_KEY 발급 필요)

주의 (2026-07-10 확인):
- 아래 BASE_URL/PATHS는 공개적으로 알려진 KRX Open API 경로 패턴을 따라 작성했다.
  실제 사용 전 반드시 마이페이지 > API 서비스 신청내역 > 명세서에서 정확한
  경로·파라미터명을 대조할 것 — 경로가 틀리면 빈 리스트/404로 바로 드러난다.
- 투자자별 순매수(외국인/기관/개인/연기금)는 이 Open API 카탈로그에 없다.
  data.krx.co.kr 정보데이터시스템 회원 로그인 기반 별도 연동(예: pykrx)이 필요 —
  get_investor_flow()는 그 사실을 '미확보'로 명확히 반환할 뿐, 데이터를 지어내지 않는다.
"""

import os
import requests

BASE_URL = 'http://data-dbg.krx.co.kr/svc/apis'

PATHS = {
    'kospi_index':  '/idx/kospi_dd_trd',   # KOSPI 시리즈 일별시세
    'kosdaq_index': '/idx/kosdaq_dd_trd',  # 코스닥 시리즈 일별시세
    'stock_kospi':  '/sto/stk_bydd_trd',   # 유가증권 개별종목 일별매매정보
    'stock_kosdaq': '/sto/ksq_bydd_trd',   # 코스닥 개별종목 일별매매정보
}


def _get_auth_key():
    key = os.environ.get('KRX_AUTH_KEY', '')
    if not key:
        print('[KRX] KRX_AUTH_KEY 없음')
    return key


def _call(path, params, auth_key=None):
    """공통 호출 헬퍼. 실패해도 빈 리스트 반환 — 한 항목 실패가 전체 수집을 막지 않게 한다."""
    auth_key = auth_key or _get_auth_key()
    if not auth_key:
        return []
    try:
        resp = requests.get(
            BASE_URL + path,
            params=params,
            headers={'AUTH_KEY': auth_key},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f'[KRX] {path} 오류: HTTP {resp.status_code} — {resp.text[:200]}')
            return []
        data = resp.json()
        # 응답 최상위 키 이름은 서비스마다 다를 수 있어(예: OutBlock_1) 리스트인 첫 값을 그대로 반환
        for v in data.values():
            if isinstance(v, list):
                return v
        return []
    except Exception as e:
        print(f'[KRX] {path} 호출 실패: {e}')
        return []


def get_kospi_index(bas_dd, auth_key=None):
    """KOSPI 지수 일별시세. bas_dd: 'YYYYMMDD'"""
    return _call(PATHS['kospi_index'], {'basDd': bas_dd}, auth_key)


def get_kosdaq_index(bas_dd, auth_key=None):
    """코스닥 지수 일별시세."""
    return _call(PATHS['kosdaq_index'], {'basDd': bas_dd}, auth_key)


def get_kospi_stock(bas_dd, isu_cd=None, auth_key=None):
    """유가증권시장 개별종목 일별매매정보. isu_cd 없으면 전체 종목(응답이 큼)."""
    params = {'basDd': bas_dd}
    if isu_cd:
        params['isuCd'] = isu_cd
    return _call(PATHS['stock_kospi'], params, auth_key)


def get_kosdaq_stock(bas_dd, isu_cd=None, auth_key=None):
    """코스닥 개별종목 일별매매정보."""
    params = {'basDd': bas_dd}
    if isu_cd:
        params['isuCd'] = isu_cd
    return _call(PATHS['stock_kosdaq'], params, auth_key)


def get_investor_flow(*_args, **_kwargs):
    """
    투자자별(외국인/기관/개인/연기금) 순매수 — Open API 카탈로그에 없음(2026-07 확인).
    data.krx.co.kr 회원 로그인 기반 별도 연동(pykrx 등) 필요.
    지어내지 않고 '미확보' 상태를 명시적으로 반환한다.
    """
    return {
        'status': 'unavailable',
        'reason': 'openapi.krx.co.kr 카탈로그에 투자자별 거래실적 없음 — pykrx/회원 로그인 연동 필요',
    }
