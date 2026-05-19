"""
감성 분석 유틸리티 — Finnhub 뉴스 감성 + 소셜 감성 수집
"""

import os
import time
import requests
from datetime import datetime
import pytz

KST = pytz.timezone('Asia/Seoul')
FINNHUB_BASE = 'https://finnhub.io/api/v1'
_H = {'User-Agent': 'Mozilla/5.0'}


def fetch_finnhub_sentiment(ticker: str) -> dict | None:
    """
    Finnhub 감성 데이터 수집.

    엔드포인트:
      1. /stock/news-sentiment  → 뉴스 감성 (buzzScore, bullishPercent, bearishPercent)
      2. /stock/social-sentiment → Reddit/Twitter 멘션 수 + 긍부정 점수

    반환 예시:
    {
        'ticker': 'TSLA',
        'news_bull_pct':  0.72,   # 0~1
        'news_bear_pct':  0.28,
        'news_buzz':      0.85,   # 0~1, 기사 빈도 지수
        'reddit_pos':     0.61,
        'reddit_neg':     0.15,
        'reddit_mention': 142,
        'twitter_pos':    0.55,
        'twitter_neg':    0.20,
        'twitter_mention':830,
        'collected_at':   '2026-05-19 09:00:00',
    }
    실패 시 None 반환.
    """
    api_key = os.environ.get('FINNHUB_API_KEY', '')
    if not api_key:
        print(f'  [Sentiment] FINNHUB_API_KEY 없음, 스킵')
        return None

    result = {
        'ticker': ticker.upper(),
        'news_bull_pct': None,
        'news_bear_pct': None,
        'news_buzz':     None,
        'reddit_pos':    None,
        'reddit_neg':    None,
        'reddit_mention':None,
        'twitter_pos':   None,
        'twitter_neg':   None,
        'twitter_mention':None,
        'collected_at':  datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
    }

    # ── 1. 뉴스 감성 ─────────────────────────────────────────────
    try:
        r = requests.get(
            f'{FINNHUB_BASE}/stock/news-sentiment',
            params={'symbol': ticker, 'token': api_key},
            headers=_H, timeout=8
        )
        if r.ok:
            d = r.json()
            sent = d.get('sentiment', {})
            buzz = d.get('buzz', {})
            result['news_bull_pct'] = sent.get('bullishPercent')
            result['news_bear_pct'] = sent.get('bearishPercent')
            result['news_buzz']     = buzz.get('buzz')
            print(f'  [Sentiment/news] {ticker}: bull={result["news_bull_pct"]}, '
                  f'bear={result["news_bear_pct"]}, buzz={result["news_buzz"]}')
        else:
            print(f'  [Sentiment/news] {ticker}: HTTP {r.status_code}')
    except Exception as e:
        print(f'  [Sentiment/news] {ticker} 오류: {e}')

    time.sleep(0.5)  # Rate limit 대비

    # ── 2. 소셜 감성 (Reddit + Twitter) ─────────────────────────
    try:
        r = requests.get(
            f'{FINNHUB_BASE}/stock/social-sentiment',
            params={'symbol': ticker, 'token': api_key},
            headers=_H, timeout=8
        )
        if r.ok:
            d = r.json()

            # Reddit: 최근 데이터 1개 사용
            reddit_list = d.get('reddit', [])
            if reddit_list:
                latest = reddit_list[-1]
                result['reddit_pos']     = latest.get('positiveScore')
                result['reddit_neg']     = latest.get('negativeScore')
                result['reddit_mention'] = latest.get('mention')

            # Twitter: 최근 데이터 1개 사용
            twitter_list = d.get('twitter', [])
            if twitter_list:
                latest = twitter_list[-1]
                result['twitter_pos']     = latest.get('positiveScore')
                result['twitter_neg']     = latest.get('negativeScore')
                result['twitter_mention'] = latest.get('mention')

            print(f'  [Sentiment/social] {ticker}: '
                  f'reddit_mention={result["reddit_mention"]}, '
                  f'twitter_mention={result["twitter_mention"]}')
        else:
            print(f'  [Sentiment/social] {ticker}: HTTP {r.status_code}')
    except Exception as e:
        print(f'  [Sentiment/social] {ticker} 오류: {e}')

    # 모든 값이 None이면 수집 실패로 간주
    values = [
        result['news_bull_pct'], result['news_bear_pct'],
        result['reddit_mention'], result['twitter_mention'],
    ]
    if all(v is None for v in values):
        print(f'  [Sentiment] {ticker}: 유효 데이터 없음, None 반환')
        return None

    return result


def fetch_all_sentiment(tickers: list[dict]) -> list[dict]:
    """
    전체 종목 감성 수집.
    tickers: [{'ticker': 'TSLA', 'company_name': 'Tesla'}, ...]
    반환: 수집 성공한 항목만 포함한 list[dict]
    """
    results = []
    total = len(tickers)
    for i, t in enumerate(tickers, 1):
        sym = t['ticker']
        print(f'  [{i}/{total}] {sym} 감성 수집 중...')
        data = fetch_finnhub_sentiment(sym)
        if data:
            results.append(data)
        time.sleep(1)  # 종목 간 Rate limit 대비

    print(f'  감성 수집 완료: {len(results)}/{total}건 성공')
    return results
