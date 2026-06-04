"""
뉴스 수집 유틸리티
Finnhub API 기반 + Gemini 번역/요약
"""

import hashlib
import json
import os
import re
import requests
import time
from datetime import datetime, timedelta
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


def translate_and_summarize(articles, model, ticker='', company_name='', today_str=''):
    """
    기사 목록을 Gemini로 번역 + 요약 (1 API 호출/종목).
    content(Finnhub snippet)가 있으면 활용, 없으면 제목 기반.

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

    articles_text = ''
    for i, a in enumerate(articles):
        if a.get('content'):
            articles_text += f"\n[기사 {i}]\n제목: {a['title']}\n내용: {a['content']}\n"
        else:
            articles_text += f"\n[기사 {i}]\n제목: {a['title']}\n내용: (스니펫 없음)\n"

    date_ctx = f"기준일: {today_str}\n" if today_str else ''
    prompt = f"""{date_ctx}다음은 Finnhub에서 수집한 {ticker}({company_name}) 관련 최신 뉴스입니다.
{articles_text}
위 Finnhub 기사들을 기반으로, Google 검색을 활용해 {ticker} 관련 추가 뉴스와 시장 분석을 보완하여 아래 JSON을 작성하세요.
검색 범위: 기준일 당일 뉴스를 우선하되, 당일 기사가 부족하면 최근 2일 이내 기사까지 포함하세요.
반드시 JSON 형식으로만 응답하세요. 다른 텍스트는 절대 포함하지 마세요.

{{
  "articles": [
    {{
      "title_kr": "기사 0 제목을 자연스러운 한국어로 번역",
      "article_summary_kr": "기사 0 핵심 내용을 500자 이내 한국어로 요약"
    }}
  ],
  "summary_kr": "Finnhub 기사와 Google 검색 결과를 종합한 최신 뉴스 브리핑. 불필요한 수식어 없이 핵심 위주로 각 섹션 2~3문장 간결하게 서술. 총 800자 이내.\\n\\n[핵심 이슈] 🔥 가장 중요한 이슈 2~3가지를 구체적 수치·사실 중심으로 서술\\n\\n[투자 포인트] 💡 투자자 관점에서 주목해야 할 내용과 리스크 요인\\n\\n[시장 분위기] 📊 전반적인 시장 및 종목 동향 — 센티멘트 평가"
}}"""

    def _parse_json(text):
        """JSON 블록 추출 (grounding 인용 마커 등 전처리)"""
        text = text.strip()
        if text.startswith('```'):
            lines = text.split('\n')
            text = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])
            if text.startswith('json'):
                text = text[4:].strip()
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            text = m.group(0)
        return json.loads(text)

    # ── 1차 시도: Google Search Grounding + Thinking 비활성 ──────
    # [C21 2026-06-04] thinking_budget=0 설정 (품질 유지, 비용 ~40% 절감)
    # Thinking($3.50/1M토큰)은 번역/JSON 변환에 불필요 → 0으로 고정
    # Grounding은 유지 (실시간 뉴스 보완 품질 유지)
    try:
        from google.genai import types
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )
        response = model.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=config
        )
        result = _parse_json(response.text)
        print(f"  [Gemini+Grounding] {ticker} 브리핑 완료")
        return {
            'articles': result.get('articles', empty['articles']),
            'summary_kr': result.get('summary_kr', '')
        }
    except Exception as e1:
        print(f"  [Gemini] Grounding 실패 ({e1}), 기본 모드로 재시도")

    # ── 2차 시도: Grounding 없이 기본 모드 ───────────────────────
    try:
        from google.genai import types
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0)
        )
        response = model.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=config
        )
        result = _parse_json(response.text)
        print(f"  [Gemini] {ticker} 브리핑 완료 (기본 모드)")
        return {
            'articles': result.get('articles', empty['articles']),
            'summary_kr': result.get('summary_kr', '')
        }
    except Exception as e2:
        print(f"  [Gemini 오류] 번역/요약 실패: {e2}")
        return empty


def fetch_news_for_ticker(ticker, company_name, max_items=10, model=None):
    """
    Finnhub API로 뉴스 수집 → Gemini 번역/요약.
    직접 기사 URL + summary snippet 제공.
    """
    api_key = os.environ.get('FINNHUB_API_KEY', '')
    if not api_key:
        print(f"  [ERROR] FINNHUB_API_KEY 없음")
        return []

    now_kst = datetime.now(KST)
    today_str = now_kst.strftime('%Y-%m-%d')
    yesterday_str = (now_kst - timedelta(days=1)).strftime('%Y-%m-%d')
    now_kst_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')

    url = (
        f"https://finnhub.io/api/v1/company-news"
        f"?symbol={ticker}&from={yesterday_str}&to={today_str}&token={api_key}"
    )

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            print(f"  [ERROR] Finnhub API 오류: {resp.status_code}")
            return []

        raw_articles = resp.json()
        if not isinstance(raw_articles, list):
            print(f"  [ERROR] Finnhub 응답 형식 오류")
            return []

        # 최신순 정렬 후 max_items 제한
        raw_articles = sorted(raw_articles, key=lambda x: x.get('datetime', 0), reverse=True)
        raw_articles = raw_articles[:max_items]

        collected = []
        for a in raw_articles:
            pub_ts = a.get('datetime', 0)
            pub_str = datetime.fromtimestamp(pub_ts, tz=KST).strftime('%Y-%m-%d %H:%M:%S') if pub_ts else ''
            article_url = a.get('url', '')
            collected.append({
                'title':    a.get('headline', ''),
                'link':     article_url,
                'published': pub_str,
                'content':  a.get('summary', ''),   # Finnhub snippet → Gemini 컨텍스트
                'source':   a.get('source', ''),
                'url_hash': hashlib.md5(article_url.encode()).hexdigest()[:12],
            })

        print(f"  {ticker} ({company_name}): {len(collected)}건 수집 (Finnhub)")

        if not collected:
            return []

        # Gemini 번역 + 요약
        summary_kr = ''
        gemini_articles = [{'title_kr': '', 'article_summary_kr': ''} for _ in collected]

        if model:
            print(f"  [Gemini] {ticker} 번역 및 요약 중...")
            gemini_input = [{'title': a['title'], 'content': a['content']} for a in collected]
            today_label = now_kst.strftime('%Y년 %m월 %d일')
            result = translate_and_summarize(gemini_input, model, ticker, company_name, today_str=today_label)
            raw_arts = result.get('articles', [])
            # collected 수에 맞게 패딩 (Gemini가 적게 반환해도 안전)
            gemini_articles = raw_arts + [{'title_kr': '', 'article_summary_kr': ''}
                                          for _ in range(max(0, len(collected) - len(raw_arts)))]
            summary_kr = result['summary_kr']

        # news_items 조합
        news_items = []
        for i, a in enumerate(collected):
            g = gemini_articles[i] if i < len(gemini_articles) else {}
            news_items.append({
                'ticker':             ticker,
                'company':            company_name,
                'title':              a['title'],
                'link':               a['link'],
                'published':          a['published'],
                'collected_at':       now_kst_str,
                'url_hash':           a['url_hash'],
                'title_kr':           g.get('title_kr', ''),
                'summary_kr':         summary_kr if i == 0 else '',
                'article_summary_kr': g.get('article_summary_kr', ''),
            })

        return news_items

    except Exception as e:
        print(f'  [ERROR] {ticker} 수집 실패: {e}')
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
