"""
전략 B(모멘텀 집중) 편입 스크리너 — 2조건(12-1 상대강도 + 50일선).

data/strategy_b_universe.json 의 종목별로:
  ① 12-1 모멘텀 = P[t-21] / P[t-252] - 1  (최근 1개월 제외, 학술 표준 모멘텀)
  ② 50일선    = 현재가 > SMA50 여부, 이격도 (현재가/SMA50 - 1)
  상대강도(RS) = 종목 12-1 모멘텀 - 해당 시장 벤치마크 12-1 모멘텀
  신호        = RS>0(벤치 초과) & 현재가>SMA50 → '편입 자격'
                둘 중 하나만 → '관찰' / 둘 다 아님 → '이탈 후보'
                (계산 불가 종목은 '데이터 부족' — 지어내지 않음)

데이터 소스는 기존 파이프라인과 동일:
  - US: Yahoo chart API (requests, 키 불필요) — us_market.py와 같은 엔드포인트
  - KR: pykrx 개별종목 시세 (로그인 불필요 — krx_scrape.py 실측 확인)

이익전망(3번째 조건)은 이 스크립트가 다루지 않는다 — 안정적 무료 소스 확보 후 별도 추가.
결과: data/strategy_b_screen.json. 실패 종목은 결과에서 빼거나 '데이터 부족'으로 남긴다.
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

UNIVERSE_PATH = 'data/strategy_b_universe.json'
OUT_PATH = 'data/strategy_b_screen.json'
KST = timezone(timedelta(hours=9))

_UA = {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36')}

# 12-1 모멘텀에 필요한 최소 거래일(약 1년). 이보다 짧으면(신규 상장 등) 모멘텀 N/A.
MIN_BARS = 252
SKIP_RECENT = 21   # 최근 1개월(≈21거래일) 제외
SMA_WINDOW = 50


def _yahoo_closes(symbol):
    """Yahoo chart API에서 1년 일별 종가 배열(옛→새 순). 실패 시 None."""
    try:
        # range=1y는 거래일 ~251개로 MIN_BARS(252)에 1개 모자라 전 종목이
        # '데이터 부족'이 되는 실측 버그가 있었다(2026-07-17) — 2y로 여유 확보.
        r = requests.get(
            f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}',
            params={'range': '2y', 'interval': '1d'},
            headers=_UA, timeout=15)
        if not r.ok:
            return None
        res = (r.json().get('chart') or {}).get('result') or []
        if not res:
            return None
        quote = (res[0].get('indicators') or {}).get('quote') or [{}]
        closes = quote[0].get('close') or []
        return [c for c in closes if c is not None]
    except Exception as e:
        print(f'[yahoo] {symbol} 실패: {e}')
        return None


_pykrx_stock = None


def _pykrx():
    global _pykrx_stock
    if _pykrx_stock is None:
        try:
            from pykrx import stock
            _pykrx_stock = stock
        except Exception as e:
            print(f'[pykrx] import 실패: {e}')
            _pykrx_stock = False
    return _pykrx_stock or None


def _krx_closes(code):
    """pykrx 개별종목 1년 일별 종가 배열(옛→새 순). 실패 시 None."""
    stock = _pykrx()
    if stock is None:
        return None
    try:
        today = datetime.now(KST).strftime('%Y%m%d')
        frm = (datetime.now(KST) - timedelta(days=400)).strftime('%Y%m%d')
        df = stock.get_market_ohlcv_by_date(frm, today, code)
        if df is None or df.empty or '종가' not in df.columns:
            return None
        return [float(x) for x in df['종가'].tolist() if x is not None and x > 0]
    except Exception as e:
        print(f'[pykrx] {code} 실패: {e}')
        return None


def _closes(market, ticker):
    if market == 'KR':
        # pykrx 우선(KRX 원천), 실패 시 Yahoo '.KS' 폴백 — 세션 컨테이너처럼
        # KRX 자격증명이 없는 환경에서 국내 종목이 통째로 '데이터 부족'이 되는 것을
        # 막는다(2026-07-17 실측: 005930.KS 종가가 KRX 확정치와 일치함을 확인).
        closes = _krx_closes(ticker)
        if closes and len(closes) >= MIN_BARS:
            return closes
        fallback = _yahoo_closes(f'{ticker}.KS')
        if fallback:
            print(f'[KR] {ticker}: pykrx 미확보 → Yahoo .KS 폴백 사용({len(fallback)}봉)')
        return fallback or closes
    return _yahoo_closes(ticker)


def _momentum_12_1(closes):
    """12-1 모멘텀. 배열이 MIN_BARS 미만이면 None(데이터 부족)."""
    if not closes or len(closes) < MIN_BARS:
        return None
    p_recent = closes[-1 - SKIP_RECENT]      # t-21
    p_old = closes[-MIN_BARS]                # ≈ t-252
    if p_old <= 0:
        return None
    return p_recent / p_old - 1.0


def _sma_state(closes):
    """(현재가>SMA50 여부, 이격도). 배열이 SMA_WINDOW 미만이면 (None, None)."""
    if not closes or len(closes) < SMA_WINDOW:
        return None, None
    sma = sum(closes[-SMA_WINDOW:]) / SMA_WINDOW
    if sma <= 0:
        return None, None
    price = closes[-1]
    return price > sma, price / sma - 1.0


def _signal(rs, above_50d):
    if rs is None or above_50d is None:
        return '데이터 부족'
    ok = int(rs > 0) + int(above_50d)
    return {2: '편입 자격', 1: '관찰'}.get(ok, '이탈 후보')


def main():
    if not os.path.exists(UNIVERSE_PATH):
        print(f'유니버스 없음: {UNIVERSE_PATH}')
        return
    with open(UNIVERSE_PATH, encoding='utf-8') as f:
        uni = json.load(f)

    # 벤치마크 12-1 모멘텀 먼저 계산
    bench_mom = {}
    for key, b in (uni.get('benchmarks') or {}).items():
        closes = _closes(b.get('market', 'US'), b['symbol'])
        bench_mom[key] = _momentum_12_1(closes)
        time.sleep(0.3)

    rows = []
    for s in uni.get('universe', []):
        closes = _closes(s['market'], s['ticker'])
        mom = _momentum_12_1(closes)
        above, gap = _sma_state(closes)
        bkey = s.get('benchmark_key', 'US')
        bm = bench_mom.get(bkey)
        rs = (mom - bm) if (mom is not None and bm is not None) else None
        rows.append({
            'name': s['name'], 'ticker': s['ticker'], 'theme': s.get('theme', ''),
            'market': s['market'], 'benchmark_key': bkey,
            'mom_12_1': mom, 'rs_vs_bench': rs,
            'above_50d': above, 'gap_50d': gap,
            'signal': _signal(rs, above),
        })
        time.sleep(0.3)

    # 신호 우선순위로 정렬(편입 자격 → 관찰 → 이탈 후보 → 데이터 부족), 그 안에선 RS 내림차순
    order = {'편입 자격': 0, '관찰': 1, '이탈 후보': 2, '데이터 부족': 3}
    rows.sort(key=lambda r: (order.get(r['signal'], 9),
                             -(r['rs_vs_bench'] if r['rs_vs_bench'] is not None else -9)))

    out = {
        'status': 'ok',
        'benchmarks': {k: {'symbol': (uni['benchmarks'][k]['symbol']), 'mom_12_1': v}
                       for k, v in bench_mom.items()},
        'rows': rows,
        'fetched_at': datetime.now(KST).strftime('%Y-%m-%d %H:%M KST'),
    }
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    ok = sum(1 for r in rows if r['signal'] != '데이터 부족')
    print(f'저장 완료: {OUT_PATH} — {ok}/{len(rows)} 종목 계산, '
          f'편입자격 {sum(1 for r in rows if r["signal"]=="편입 자격")}개')


if __name__ == '__main__':
    main()
