"""
일일 종합 브리핑 생성 — KST 07:00 (UTC 22:00) 실행
GitHub Actions: .github/workflows/daily_briefing.yml

흐름:
  1. TODAY 시트에서 종목별 번역된 기사 목록 조회
  2. 종목별 Gemini 호출 → summary_kr 생성 (Grounding 활용, Thinking=0)
  3. TODAY 시트의 해당 종목 첫 번째 행 summary_kr 업데이트
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
import pytz
import json
import re

from utils.sheets import get_tickers, get_today_news, update_daily_summary

KST = pytz.timezone('Asia/Seoul')


def _get_gemini_client():
    api_key = os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        print('[Gemini] GEMINI_API_KEY 없음')
        return None
    try:
        from google import genai
        return genai.Client(api_key=api_key)
    except Exception as e:
        print(f'[Gemini] 초기화 실패: {e}')
        return None


def generate_summary(ticker, company_name, articles_df, model, today_str):
    """
    번역된 기사 목록으로 종합 브리핑 생성.
    articles_df: DataFrame (title_kr, article_summary_kr 컬럼 포함)
    """
    if articles_df.empty:
        print(f'  {ticker}: 오늘 기사 없음 — 브리핑 스킵')
        return None

    articles_text = ''
    for i, (_, row) in enumerate(articles_df.iterrows()):
        title = str(row.get('title_kr') or row.get('title') or '').strip()
        summary = str(row.get('article_summary_kr', '') or '').strip()
        if title:
            articles_text += f"\n[기사 {i}]\n제목: {title}\n"
            if summary:
                articles_text += f"요약: {summary}\n"

    if not articles_text.strip():
        print(f'  {ticker}: 번역 내용 없음 — 브리핑 스킵')
        return None

    prompt = f"""기준일: {today_str}
다음은 {ticker}({company_name}) 관련 오늘의 뉴스 기사 목록입니다.
{articles_text}
위 기사들과 Google 검색을 활용해 {ticker} 관련 최신 시장 동향을 종합하여 아래 형식으로 한국어 브리핑을 작성하세요.
불필요한 수식어 없이 핵심 위주로 각 섹션 2~3문장, 총 800자 이내.
JSON 형식으로만 응답하세요.

{{
  "summary_kr": "[핵심 이슈] 🔥 가장 중요한 이슈 2~3가지를 구체적 수치·사실 중심으로 서술\\n\\n[투자 포인트] 💡 투자자 관점에서 주목해야 할 내용과 리스크 요인\\n\\n[시장 분위기] 📊 전반적인 시장 및 종목 동향 — 센티멘트 평가"
}}"""

    def _parse(text):
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

    # Grounding 활성 + Thinking 비활성
    try:
        from google.genai import types
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )
        resp = model.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=config
        )
        result = _parse(resp.text)
        summary = result.get('summary_kr', '').strip()
        print(f'  {ticker}: 브리핑 생성 완료 ({len(summary)}자)')
        return summary
    except Exception as e1:
        print(f'  {ticker}: Grounding 실패 ({e1}), 기본 모드 재시도')

    # 폴백: Grounding 없이
    try:
        from google.genai import types
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0)
        )
        resp = model.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=config
        )
        result = _parse(resp.text)
        summary = result.get('summary_kr', '').strip()
        print(f'  {ticker}: 브리핑 생성 완료 (기본 모드, {len(summary)}자)')
        return summary
    except Exception as e2:
        print(f'  {ticker}: 브리핑 생성 실패: {e2}')
        return None


def main():
    now = datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')
    today_label = datetime.now(KST).strftime('%Y년 %m월 %d일')
    print(f'[{now}] 일일 종합 브리핑 생성 시작')
    print('=' * 50)

    model = _get_gemini_client()
    if not model:
        print('Gemini 초기화 실패 — 종료')
        sys.exit(1)

    tickers  = get_tickers()
    today_df = get_today_news()

    if today_df.empty:
        print('TODAY 시트에 기사 없음 — 종료')
        return

    success, skip = 0, 0
    for t in tickers:
        sym  = t['ticker']
        name = t.get('company_name', sym)
        ticker_df = today_df[today_df['ticker'] == sym].copy() if 'ticker' in today_df.columns else today_df.iloc[0:0]

        print(f'\n[{sym}] {name} — {len(ticker_df)}건')
        summary = generate_summary(sym, name, ticker_df, model, today_label)

        if summary:
            ok = update_daily_summary(sym, summary)
            if ok:
                success += 1
            else:
                skip += 1
        else:
            skip += 1

    print('\n' + '=' * 50)
    print(f'✅ 완료: {success}종목 브리핑 저장 / {skip}종목 스킵')


if __name__ == '__main__':
    main()
