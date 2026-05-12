"""
자정 초기화 스크립트 — GitHub Actions에서 KST 자정(UTC 15:00)에 실행
TODAY 시트 → 종목별 아카이브, 90일 초과 데이터 삭제, TODAY 초기화
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from utils.sheets import archive_and_reset
from datetime import datetime
import pytz

KST = pytz.timezone('Asia/Seoul')


def main():
    now = datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')
    print(f"[{now}] 자정 아카이빙 시작")
    print("=" * 50)

    archive_and_reset()

    print("=" * 50)
    print("✅ 자정 아카이빙 완료")


if __name__ == '__main__':
    main()
