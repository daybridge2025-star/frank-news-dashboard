"""
뉴스 수집 스크립트 — GitHub Actions에서 매시 정각 실행
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from utils.sheets import get_tickers, save_news_to_today
from utils.news import fetch_all_news
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


if __name__ == '__main__':
    main()
