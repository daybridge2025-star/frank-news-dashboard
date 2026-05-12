"""
뉴스 수집 유틸리티
Google News RSS 기반
"""

import feedparser
import hashlib
import time
from datetime import datetime
import pytz

KST = pytz.timezone('Asia/Seoul')


def fetch_news_for_ticker(ticker, company_name, max_items=10):
    """Google News RSS에서 특정 종목 뉴스 수집"""
    query = f"{ticker}+stock"
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

    try:
        feed = feedparser.parse(url)
        now_kst = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')

        news_items = []
        for entry in feed.entries[:max_items]:
            url_hash = hashlib.md5(entry.link.encode()).hexdigest()[:12]
            news_items.append({
                'ticker': ticker,
                'company': company_name,
                'title': entry.title,
                'link': entry.link,
                'published': entry.get('published', ''),
                'collected_at': now_kst,
                'url_hash': url_hash
            })

        print(f"  {ticker} ({company_name}): {len(news_items)}건 수집")
        return news_items

    except Exception as e:
        print(f"  [ERROR] {ticker} 뉴스 수집 실패: {e}")
        return []


def fetch_all_news(tickers, delay=0.5):
    """
    전체 종목 뉴스 일괄 수집
    delay: 종목 간 요청 딜레이 (초) - Rate limiting 방지
    """
    all_news = []
    for t in tickers:
        items = fetch_news_for_ticker(t['ticker'], t['company_name'])
        all_news.extend(items)
        if delay > 0:
            time.sleep(delay)
    return all_news
