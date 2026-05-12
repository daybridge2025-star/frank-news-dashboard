"""
Google Sheets 연동 유틸리티
OAuth refresh token 방식으로 인증
"""

import os
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import pandas as pd
from datetime import datetime, timedelta
import pytz

KST = pytz.timezone('Asia/Seoul')

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

DEFAULT_TICKERS = [
    {'ticker': 'TSLA', 'company_name': '테슬라'},
    {'ticker': 'RKLB', 'company_name': '로켓랩'},
    {'ticker': 'PLTR', 'company_name': '팔란티어'},
    {'ticker': 'SATL', 'company_name': '세틀로직'},
    {'ticker': 'VST',  'company_name': '비스트라 에너지'},
    {'ticker': 'IONQ', 'company_name': '아이온큐'},
    {'ticker': 'ONDS', 'company_name': '온다스'},
    {'ticker': 'KTOS', 'company_name': '크라토스'},
]

TODAY_HEADERS = ['ticker', 'company', 'title', 'link', 'published', 'collected_at', 'url_hash']
CONFIG_HEADERS = ['ticker', 'company_name', 'added_date']


def get_gspread_client():
    """OAuth refresh token으로 gspread 클라이언트 생성"""
    creds = Credentials(
        token=None,
        refresh_token=os.environ['GOOGLE_REFRESH_TOKEN'],
        client_id=os.environ['GOOGLE_CLIENT_ID'],
        client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
        token_uri=os.environ['GOOGLE_TOKEN_URI'],
        scopes=SCOPES
    )
    creds.refresh(Request())
    return gspread.authorize(creds)


def get_spreadsheet():
    client = get_gspread_client()
    return client.open_by_key(os.environ['GOOGLE_SHEETS_ID'])


def _ensure_sheet(ss, name, headers, rows=2000, cols=10):
    """시트가 없으면 생성 후 헤더 추가"""
    try:
        sheet = ss.worksheet(name)
    except gspread.WorksheetNotFound:
        sheet = ss.add_worksheet(name, rows, cols)
        sheet.append_row(headers)
    return sheet


def get_tickers():
    """CONFIG 시트에서 종목 목록 반환. 없으면 기본값으로 초기화."""
    try:
        ss = get_spreadsheet()
        try:
            config = ss.worksheet('CONFIG')
            records = config.get_all_records()
            if records:
                return records
        except gspread.WorksheetNotFound:
            config = ss.add_worksheet('CONFIG', 200, 3)
            config.append_row(CONFIG_HEADERS)

        today_str = datetime.now(KST).strftime('%Y-%m-%d')
        rows = [[t['ticker'], t['company_name'], today_str] for t in DEFAULT_TICKERS]
        config.append_rows(rows)
        return DEFAULT_TICKERS

    except Exception as e:
        print(f"[ERROR] get_tickers: {e}")
        return DEFAULT_TICKERS


def add_ticker(ticker, company_name):
    """종목 추가"""
    ss = get_spreadsheet()
    config = _ensure_sheet(ss, 'CONFIG', CONFIG_HEADERS, 200, 3)
    today_str = datetime.now(KST).strftime('%Y-%m-%d')
    config.append_row([ticker.upper().strip(), company_name.strip(), today_str])


def remove_ticker(ticker):
    """종목 삭제"""
    ss = get_spreadsheet()
    try:
        config = ss.worksheet('CONFIG')
    except gspread.WorksheetNotFound:
        return
    records = config.get_all_records()
    for i, r in enumerate(records):
        if r['ticker'] == ticker.upper():
            config.delete_rows(i + 2)
            return


def get_today_news():
    """TODAY 시트 전체 데이터를 DataFrame으로 반환"""
    try:
        ss = get_spreadsheet()
        try:
            today_sheet = ss.worksheet('TODAY')
        except gspread.WorksheetNotFound:
            return pd.DataFrame(columns=TODAY_HEADERS)

        records = today_sheet.get_all_records()
        if not records:
            return pd.DataFrame(columns=TODAY_HEADERS)
        return pd.DataFrame(records)

    except Exception as e:
        print(f"[ERROR] get_today_news: {e}")
        return pd.DataFrame(columns=TODAY_HEADERS)


def get_existing_hashes(today_sheet):
    """TODAY 시트의 url_hash 집합 반환 (중복 방지용)"""
    try:
        hashes = today_sheet.col_values(7)
        return set(hashes[1:])
    except Exception:
        return set()


def save_news_to_today(news_items):
    """신규 뉴스를 TODAY 시트에 저장. 저장된 건수 반환."""
    if not news_items:
        return 0

    try:
        ss = get_spreadsheet()
        today_sheet = _ensure_sheet(ss, 'TODAY', TODAY_HEADERS, 2000, 7)
        existing = get_existing_hashes(today_sheet)

        new_rows = []
        for item in news_items:
            if item['url_hash'] not in existing:
                new_rows.append([
                    item['ticker'], item['company'], item['title'],
                    item['link'], item['published'], item['collected_at'], item['url_hash']
                ])
                existing.add(item['url_hash'])

        if new_rows:
            today_sheet.append_rows(new_rows, value_input_option='RAW')

        return len(new_rows)

    except Exception as e:
        print(f"[ERROR] save_news_to_today: {e}")
        return 0


def archive_and_reset():
    """
    자정 작업:
    1. TODAY 시트 → 종목별 아카이브 시트로 이동
    2. 각 아카이브 시트에서 90일 초과 데이터 삭제
    3. TODAY 시트 초기화
    """
    ss = get_spreadsheet()
    cutoff = datetime.now(KST) - timedelta(days=90)

    try:
        today_sheet = ss.worksheet('TODAY')
    except gspread.WorksheetNotFound:
        print("TODAY 시트 없음, 스킵")
        return

    records = today_sheet.get_all_records()
    if not records:
        print("TODAY 시트 비어있음, 초기화만 수행")
        today_sheet.clear()
        today_sheet.append_row(TODAY_HEADERS)
        return

    df = pd.DataFrame(records)

    for ticker in df['ticker'].unique():
        ticker_df = df[df['ticker'] == ticker]
        archive = _ensure_sheet(ss, ticker, TODAY_HEADERS, 5000, 7)

        existing = get_existing_hashes(archive)
        new_rows = []
        for _, row in ticker_df.iterrows():
            if str(row['url_hash']) not in existing:
                new_rows.append([
                    row['ticker'], row['company'], row['title'],
                    row['link'], row['published'], row['collected_at'], row['url_hash']
                ])

        if new_rows:
            archive.append_rows(new_rows, value_input_option='RAW')
            print(f"  {ticker}: {len(new_rows)}건 아카이브")

        all_records = archive.get_all_records()
        keep, deleted = [], 0
        for r in all_records:
            try:
                dt = datetime.strptime(r['collected_at'], '%Y-%m-%d %H:%M:%S')
                dt = KST.localize(dt)
                if dt >= cutoff:
                    keep.append(r)
                else:
                    deleted += 1
            except Exception:
                keep.append(r)

        if deleted > 0:
            archive.clear()
            archive.append_row(TODAY_HEADERS)
            if keep:
                rows = [[r['ticker'], r['company'], r['title'],
                         r['link'], r['published'], r['collected_at'], r['url_hash']]
                        for r in keep]
                archive.append_rows(rows, value_input_option='RAW')
            print(f"  {ticker}: {deleted}건 90일 초과 삭제")

    today_sheet.clear()
    today_sheet.append_row(TODAY_HEADERS)
    print("TODAY 시트 초기화 완료")
