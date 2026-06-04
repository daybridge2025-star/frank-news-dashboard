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
    신규 기사 목록을 Gemini로 번역 (1 API 호출/종목).
    summary_kr은 생성하지 않음 — 일일 브리핑은 daily_briefing.py에서 별도 생성.

    articles: [{'title': str, 'content': str or None}, ...]
    반환: {
        'articles': [{'title_kr': str, 'article_summary_kr': str}, ...],
    }
    """
    empty = {'articles': [{'title_kr': '', 'article_summary_kr': ''} for _ in articles]}
    if not model or not articles:
        return empty

    articles_text = ''
    for i, a in enumerate(articles):
        if a.get('content'):
            articles_text += f"\n[기사 {i}]\n제목: {a['title']}\n내용: {a['content']}\n"
        else:
            articles_text += f"\n[기사 {i}]\n제목: {a['title']}\n내용: (스니펫 없음)\n"

    date_ctx = f"기준일: {today_str}\n" if today_str else ''
    prompt = f"""{date_ctx}다음은 {ticker}({company_name}) 관련 최신 뉴스입니다.
{articles_text}
각 기사를 한국어로 번역·요약하여 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 절대 포함하지 마세요.

{{
  "articles": [
    {{
      "title_kr": "기사 0 제목을 자연스러운 한국어로 번역",
      "article_summary_kr": "기사 0 핵심 내용을 500자 이내 한국어로 요약"
    }}
  ]
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

    # ── 번역 전용 모드 (Thinking 비활성, Grounding 불필요) ───────
    # 개별 기사 번역은 단순 변환 작업 → thinking/grounding 모두 불필요
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
        print(f"  [Gemini] {ticker} 번역 완료 ({len(result.get('articles', []))}건)")
        return {'articles': result.get('articles', empty['articles'])}
    except Exception as e:
        print(f"  [Gemini 오류] 번역 실패: {e}")
        return empty


def fetch_news_for_ticker(ticker, company_name, max_items=10, model=None, existing_hashes=None):
    """
    Finnhub API로 뉴스 수집 → 신규 기사만 Gemini 번역.
    existing_hashes: 이미 Sheets에 저장된 url_hash set — 신규 기사만 Gemini 호출.
    summary_kr은 생성하지 않음 (daily_briefing.py에서 KST 07:00에 별도 생성).
    """
    if existing_hashes is None:
        existing_hashes = set()
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

        # 신규 기사 판별
        new_articles  = [a for a in collected if a['url_hash'] not in existing_hashes]
        seen_articles = [a for a in collected if a['url_hash'] in existing_hashes]
        print(f"  {ticker}: 신규 {len(new_articles)}건 / 기존 {len(seen_articles)}건")

        # 신규 기사가 없으면 Gemini 호출 없이 종료
        if not new_articles:
            print(f"  {ticker}: 신규 기사 없음 — Gemini 스킵")
            return []

        # 신규 기사만 Gemini 번역 (summary_kr은 daily_briefing.py에서 생성)
        gemini_map = {}  # url_hash → {title_kr, article_summary_kr}
        if model:
            today_label  = now_kst.strftime('%Y년 %m월 %d일')
            gemini_input = [{'title': a['title'], 'content': a['content']} for a in new_articles]
            result       = translate_and_summarize(gemini_input, model, ticker, company_name, today_str=today_label)
            raw_arts     = result.get('articles', [])
            for i, a in enumerate(new_articles):
                gemini_map[a['url_hash']] = raw_arts[i] if i < len(raw_arts) else {}

        # 신규 기사만 news_items 조합 (summary_kr은 빈 값 — 브리핑은 07:00에 생성)
        news_items = []
        for a in new_articles:
            g = gemini_map.get(a['url_hash'], {})
            news_items.append({
                'ticker':             ticker,
                'company':            company_name,
                'title':              a['title'],
                'link':               a['link'],
                'published':          a['published'],
                'collected_at':       now_kst_str,
                'url_hash':           a['url_hash'],
                'title_kr':           g.get('title_kr', ''),
                'summary_kr':         '',  # 일일 브리핑 스크립트(KST 07:00)에서 채움
                'article_summary_kr': g.get('article_summary_kr', ''),
            })

        return news_items

    except Exception as e:
        print(f'  [ERROR] {ticker} 수집 실패: {e}')
        return []



def fetch_all_news(tickers, delay=1.0):
    """전체 종목 뉴스 일괄 수집 — 신규 기사만 Gemini 번역."""
    from utils.sheets import get_today_hashes_set
    model         = _get_gemini_model()
    existing      = get_today_hashes_set()  # Sheets 기존 hash 선취득
    print(f"[중복 체크] 기존 저장 기사: {len(existing)}건")
    all_news = []
    for t in tickers:
        items = fetch_news_for_ticker(
            t['ticker'], t['company_name'],
            model=model, existing_hashes=existing
        )
        all_news.extend(items)
        # 신규 hash를 existing에 추가 (동일 run 내 중복 방지)
        for item in items:
            existing.add(item['url_hash'])
        if delay > 0:
            time.sleep(delay)
    return all_news
