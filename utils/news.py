"""
뉴스 수집 유틸리티
Google News RSS 기반 + Gemini 번역/요약
"""

import feedparser
import hashlib
import json
import os
import time
from datetime import datetime
import pytz

KST = pytz.timezone('Asia/Seoul')


def _get_gemini_model():
    """Gemini 모델 초기화. API 키 없으면 None 반환."""
    api_key = os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        print("  [Gemini] GEMINI_API_KEY 없음 - 번역 스킵")
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        print("  [Gemini] 클라이언트 초기화 완료")
        return client
    except Exception as e:
        print(f"  [Gemini] 초기화 실패: {e}")
        return None


def translate_and_summarize(news_items, model):
    """
    뉴스 제목 목록을 한꺼번에 번역 + 요약 (1 API 호출/종목)
    반환: {0: {'title_kr': '...', 'summary_kr': '...'}, 1: ...}
    """
    if not model or not news_items:
        return {i: {'title_kr': '', 'summary_kr': ''} for i in range(len(news_items))}

    titles = [item['title'] for item in news_items]
    numbered = '\n'.join(f"{i}. {t}" for i, t in enumerate(titles))

    prompt = f"""다음 미국 주식 뉴스 제목들을 한국어로 번역하고, 각각 한 줄 요약을 작성해주세요.

{numbered}

반드시 아래 JSON 배열 형식으로만 응답하세요. 다른 텍스트는 절대 포함하지 마세요:
[
  {{"title_kr": "한국어 번역 제목", "summary_kr": "한 줄 요약 (40자 이내)"}},
  ...
]"""

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()

        # 코드블록 제거
        if text.startswith('```'):
            lines = text.split('\n')
            text = '\n'.join(lines[1:-1] if lines[-1] == '```' else lines[1:])
            if text.startswith('json'):
                text = text[4:].strip()

        results = json.loads(text.strip())
        return {i: results[i] for i in range(min(len(results), len(news_items)))}

    except Exception as e:
        print(f"  [Gemini 오류] 번역 실패: {e}")
        return {i: {'title_kr': '', 'summary_kr': ''} for i in range(len(news_items))}


def fetch_news_for_ticker(ticker, company_name, max_items=10, model=None):
    """Google News RSS에서 특정 종목 뉴스 수집 + 번역"""
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
                'url_hash': url_hash,
                'title_kr': '',
                'summary_kr': '',
            })

        print(f"  {ticker} ({company_name}): {len(news_items)}건 수집")

        # Gemini 번역/요약
        if model and news_items:
            print(f"  [Gemini] {ticker} 번역 중 ({len(news_items)}건)...")
            translations = translate_and_summarize(news_items, model)
            for i, item in enumerate(news_items):
                t = translations.get(i, {'title_kr': '', 'summary_kr': ''})
                item['title_kr'] = t.get('title_kr', '')
                item['summary_kr'] = t.get('summary_kr', '')

        return news_items

    except Exception as e:
        print(f"  [ERROR] {ticker} 뉴스 수집 실패: {e}")
        return []


def fetch_all_news(tickers, delay=1.0):
    """
    전체 종목 뉴스 일괄 수집
    delay: 종목 간 요청 딜레이 (초) - Rate limiting 방지
    """
    model = _get_gemini_model()

    all_news = []
    for t in tickers:
        items = fetch_news_for_ticker(t['ticker'], t['company_name'], model=model)
        all_news.extend(items)
        if delay > 0:
            time.sleep(delay)
    return all_news
