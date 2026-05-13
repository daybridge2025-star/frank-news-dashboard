"""
뉴스 수집 유틸리티
Google News RSS 기반 + 기사 본문 크롤링(가능한 경우) + Gemini 번역/요약
"""

import feedparser
import hashlib
import json
import os
import time
import requests
from datetime import datetime
import pytz

KST = pytz.timezone('Asia/Seoul')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}


def _get_gemini_model():
    """Gemini 클라이언트 초기화. API 키 없으면 None 반환."""
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


def fetch_article_content(url, timeout=10):
    """
    기사 URL에서 본문 크롤링 시도.
    페이월/차단/빈 본문이면 None 반환 (기사 수집은 계속).
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return None

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, 'html.parser')

        for tag in soup(['script', 'style', 'nav', 'header', 'footer',
                         'aside', 'iframe', 'form', 'noscript']):
            tag.decompose()

        article_tag = soup.find('article')
        if article_tag:
            text = article_tag.get_text(separator=' ', strip=True)
        else:
            paragraphs = soup.find_all('p')
            text = ' '.join(p.get_text(strip=True) for p in paragraphs)

        text = ' '.join(text.split())

        if len(text) < 200:
            return None

        return text[:4000]

    except Exception:
        return None


def translate_and_summarize(articles, model, ticker='', company_name=''):
    """
    기사 목록을 Gemini로 번역 + 요약 (1 API 호출/종목).
    content가 없는 기사는 제목만으로 번역 + 요약.

    articles: [{'title': str, 'content': str or None}, ...]
    반환: {
        'articles': [{'title_kr': str, 'article_summary_kr': str}, ...],
        'summary_kr': str
    }
    """
    empty = {
        'articles': [{'title_kr': '', 'article_summary_kr': ''} for _ in articles],
        'summary_kr': ''
    }
    if not model or not articles:
        return empty

    # 기사별 텍스트 구성 (본문 있으면 본문, 없으면 제목만)
    articles_text = ''
    for i, a in enumerate(articles):
        if a.get('content'):
            articles_text += f"\n[기사 {i}]\n제목: {a['title']}\n본문: {a['content']}\n"
        else:
            articles_text += f"\n[기사 {i}]\n제목: {a['title']}\n본문: (본문 없음 - 제목 기반으로 요약)\n"

    prompt = f"""다음은 {ticker}({company_name}) 관련 최신 미국 주식 뉴스입니다.
{articles_text}
아래 두 가지 작업을 수행하고, 반드시 JSON 형식으로만 응답하세요. 다른 텍스트는 절대 포함하지 마세요.

{{
  "articles": [
    {{
      "title_kr": "기사 0 제목을 자연스러운 한국어로 번역",
      "article_summary_kr": "기사 0 핵심 내용을 500자 이내 한국어로 요약 (본문 없으면 제목 기반으로 작성)"
    }}
  ],
  "summary_kr": "전체 기사를 종합 분석한 오늘의 뉴스 동향 (1000자 이내)\\n\\n[핵심 이슈] 오늘 가장 중요한 이슈 2~3가지를 구체적으로 서술\\n\\n[투자 포인트] 투자자 관점에서 주목해야 할 내용과 리스크 요인\\n\\n[시장 분위기] 전반적인 시장 및 종목 동향 평가"
}}"""

    try:
        response = model.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        text = response.text.strip()

        if text.startswith('```'):
            lines = text.split('\n')
            text = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])
            if text.startswith('json'):
                text = text[4:].strip()

        result = json.loads(text.strip())
        return {
            'articles': result.get('articles', empty['articles']),
            'summary_kr': result.get('summary_kr', '')
        }

    except Exception as e:
        print(f"  [Gemini 오류] 번역/요약 실패: {e}")
        return empty


def fetch_news_for_ticker(ticker, company_name, max_items=10, model=None):
    """
    Google News RSS 수집 → 기사 본문 크롤링 시도(실패해도 기사 유지)
    → Gemini 번역/요약.
    """
    query = f"{ticker}+stock"
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

    try:
        feed = feedparser.parse(url)
        now_kst = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')

        # 1단계: RSS 수집 + 본문 크롤링 시도 (실패해도 기사는 유지)
        collected = []
        crawl_ok = 0
        for entry in feed.entries[:max_items]:
            content = fetch_article_content(entry.link)
            if content:
                crawl_ok += 1
            collected.append({
                'title': entry.title,
                'link': entry.link,
                'published': entry.get('published', ''),
                'content': content,  # None이면 제목 기반 요약
                'url_hash': hashlib.md5(entry.link.encode()).hexdigest()[:12],
            })

        print(f"  {ticker} ({company_name}): {len(collected)}건 수집 "
              f"(본문 크롤링 성공 {crawl_ok}건 / 제목 기반 {len(collected)-crawl_ok}건)")

        if not collected:
            return []

        # 2단계: Gemini 번역 + 요약
        summary_kr = ''
        gemini_articles = [{'title_kr': '', 'article_summary_kr': ''} for _ in collected]

        if model:
            print(f"  [Gemini] {ticker} 번역 및 요약 중...")
            gemini_input = [{'title': a['title'], 'content': a['content']} for a in collected]
            result = translate_and_summarize(gemini_input, model, ticker, company_name)
            gemini_articles = result['articles']
            summary_kr = result['summary_kr']

        # 3단계: news_items 조합
        news_items = []
        for i, a in enumerate(collected):
            g = gemini_articles[i] if i < len(gemini_articles) else {}
            news_items.append({
                'ticker': ticker,
                'company': company_name,
                'title': a['title'],
                'link': a['link'],
                'published': a['published'],
                'collected_at': now_kst,
                'url_hash': a['url_hash'],
                'title_kr': g.get('title_kr', ''),
                'summary_kr': summary_kr if i == 0 else '',
                'article_summary_kr': g.get('article_summary_kr', ''),
            })

        return news_items

    except Exception as e:
        print(f"  [ERROR] {ticker} 수집 실패: {e}")
        return []


def fetch_all_news(tickers, delay=1.0):
    """전체 종목 뉴스 일괄 수집"""
    model = _get_gemini_model()

    all_news = []
    for t in tickers:
        items = fetch_news_for_ticker(t['ticker'], t['company_name'], model=model)
        all_news.extend(items)
        if delay > 0:
            time.sleep(delay)
    return all_news
