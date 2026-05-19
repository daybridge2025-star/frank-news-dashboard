"""
뉴스 수집 스크립트 — GitHub Actions에서 매시 정각 실행
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from utils.sheets import get_tickers, save_news_to_today, save_sentiment
from utils.news import fetch_all_news
from utils.sentiment import fetch_all_sentiment
from datetime import datetime
import pytz

KST = pytz.timezone('Asia/Seoul')


def main():
    now = datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')
    print(f"[{now}] 뉴스 수집 시작")
    print("=" * 50)

    # 종목 목록 로드
    tickers = get_tickers()
    if not tickers:
        print("수집할 종목 없음. 종료.")
        return

    ticker_list = [t['ticker'] for t in tickers]
    print(f"대상 {len(tickers)}개 종목: {', '.join(ticker_list)}")
    print()

    # 뉴스 수집
    all_news = fetch_all_news(tickers)
    print()

    # Google Sheets 저장
    saved = save_news_to_today(all_news)

    print("=" * 50)
    print(f"✅ 완료: 총 {len(all_news)}건 수집 / {saved}건 신규 저장")
    print(f"   (중복 제외: {len(all_news) - saved}건 스킵)")

    # ── 감성 수집 (뉴스 수집과 완전 독립 — 실패해도 뉴스 결과 영향 없음) ──
    print()
    print("── 감성 분석 수집 시작 ──")
    try:
        sentiment_list = fetch_all_sentiment(tickers)
        if sentiment_list:
            s_saved = save_sentiment(sentiment_list)
            print(f"✅ 감성 수집 완료: {len(sentiment_list)}건 수집 / {s_saved}건 저장")
        else:
            print("⚠️  감성 수집 결과 없음 (Finnhub 응답 확인 필요)")
    except Exception as e:
        print(f"⚠️  감성 수집 실패 (뉴스 저장에는 영향 없음): {e}")


if __name__ == '__main__':
    main()
