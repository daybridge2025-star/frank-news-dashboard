"""
Google Sheets utility
OAuth refresh token authentication
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
    {'ticker': 'TSLA', 'company_name': 'Tesla'},
    {'ticker': 'RKLB', 'company_name': 'Rocket Lab'},
    {'ticker': 'PLTR', 'company_name': 'Palantir'},
    {'ticker': 'SATL', 'company_name': 'Satellogic'},
    {'ticker': 'VST',  'company_name': 'Vistra'},
    {'ticker': 'IONQ', 'company_name': 'IonQ'},
    {'ticker': 'ONDS', 'company_name': 'Ondas'},
    {'ticker': 'KTOS', 'company_name': 'Kratos'},
]

TODAY_HEADERS = [
    'ticker', 'company', 'title', 'link', 'published',
    'collected_at', 'url_hash', 'title_kr', 'summary_kr', 'article_summary_kr'
]
CONFIG_HEADERS = ['ticker', 'company_name', 'added_date']

SENTIMENT_HEADERS = [
    'ticker', 'date',
    'news_bull_pct', 'news_bear_pct', 'news_buzz',
    'reddit_pos', 'reddit_neg', 'reddit_mention',
    'twitter_pos', 'twitter_neg', 'twitter_mention',
    'collected_at',
]


def get_gspread_client():
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
    try:
        sheet = ss.worksheet(name)
    except gspread.WorksheetNotFound:
        sheet = ss.add_worksheet(name, rows, cols)
        sheet.append_row(headers)
    return sheet


def get_tickers():
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
    ss = get_spreadsheet()
    config = _ensure_sheet(ss, 'CONFIG', CONFIG_HEADERS, 200, 3)
    today_str = datetime.now(KST).strftime('%Y-%m-%d')
    config.append_row([ticker.upper().strip(), company_name.strip(), today_str])


def remove_ticker(ticker):
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


def reorder_tickers(ordered_list):
    """
    Reorder tickers by rewriting the entire CONFIG sheet.
    ordered_list: list of dicts with 'ticker', 'company_name', 'added_date'
    Extensible: add new fields (major shareholders, CEO, etc.) here only.
    """
    ss = get_spreadsheet()
    config = _ensure_sheet(ss, 'CONFIG', CONFIG_HEADERS, 200, 3)
    config.clear()
    config.append_row(CONFIG_HEADERS)
    rows = [
        [t['ticker'], t.get('company_name', ''), t.get('added_date', '')]
        for t in ordered_list
    ]
    if rows:
        config.append_rows(rows, value_input_option='RAW')


def get_today_news():
    try:
        ss = get_spreadsheet()
        try:
            today_sheet = ss.worksheet('TODAY')
        except gspread.WorksheetNotFound:
            return pd.DataFrame(columns=TODAY_HEADERS)

        records = today_sheet.get_all_records(expected_headers=TODAY_HEADERS)
        if not records:
            return pd.DataFrame(columns=TODAY_HEADERS)
        return pd.DataFrame(records)

    except Exception as e:
        print(f"[ERROR] get_today_news: {e}")
        return pd.DataFrame(columns=TODAY_HEADERS)


def get_existing_hashes(today_sheet):
    try:
        hashes = today_sheet.col_values(7)
        return set(hashes[1:])
    except Exception:
        return set()


def save_news_to_today(news_items):
    if not news_items:
        return 0
    try:
        ss = get_spreadsheet()
        today_sheet = _ensure_sheet(ss, 'TODAY', TODAY_HEADERS, 2000, 10)
        existing = get_existing_hashes(today_sheet)
        new_rows = []
        for item in news_items:
            if item['url_hash'] not in existing:
                new_rows.append([
                    item['ticker'], item['company'], item['title'],
                    item['link'], item['published'], item['collected_at'], item['url_hash'],
                    item.get('title_kr', ''), item.get('summary_kr', ''),
                    item.get('article_summary_kr', '')
                ])
                existing.add(item['url_hash'])
        if new_rows:
            today_sheet.append_rows(new_rows, value_input_option='RAW')
        return len(new_rows)
    except Exception as e:
        print(f"[ERROR] save_news_to_today: {e}")
        return 0


def save_sentiment(sentiment_items: list) -> int:
    """
    감성 수집 결과를 SENTIMENT 시트에 저장.
    동일 ticker+date 행이 있으면 업데이트, 없으면 추가.
    반환: 저장된 행 수
    """
    if not sentiment_items:
        return 0
    try:
        ss = get_spreadsheet()
        sheet = _ensure_sheet(ss, 'SENTIMENT', SENTIMENT_HEADERS, 500, len(SENTIMENT_HEADERS))

        today_str = datetime.now(KST).strftime('%Y-%m-%d')

        # 기존 데이터 로드 (ticker+date 키로 행 번호 인덱스)
        all_values = sheet.get_all_values()
        header_row = all_values[0] if all_values else SENTIMENT_HEADERS
        # ticker col=0, date col=1
        existing_keys = {}  # (ticker, date) -> row_number (1-based, 헤더=1이므로 +2)
        for row_idx, row in enumerate(all_values[1:], start=2):
            if len(row) >= 2:
                key = (row[0], row[1])
                existing_keys[key] = row_idx

        saved = 0
        for item in sentiment_items:
            key = (item['ticker'].upper(), today_str)
            row_data = [
                item['ticker'].upper(),
                today_str,
                item.get('news_bull_pct', ''),
                item.get('news_bear_pct', ''),
                item.get('news_buzz', ''),
                item.get('reddit_pos', ''),
                item.get('reddit_neg', ''),
                item.get('reddit_mention', ''),
                item.get('twitter_pos', ''),
                item.get('twitter_neg', ''),
                item.get('twitter_mention', ''),
                item.get('collected_at', ''),
            ]
            # None → 빈 문자열 변환
            row_data = ['' if v is None else v for v in row_data]

            if key in existing_keys:
                # 기존 행 업데이트
                sheet.update(f'A{existing_keys[key]}', [row_data])
            else:
                # 신규 행 추가
                sheet.append_row(row_data, value_input_option='RAW')
            saved += 1

        return saved
    except Exception as e:
        print(f'[ERROR] save_sentiment: {e}')
        return 0


def get_sentiment() -> 'pd.DataFrame':
    """
    SENTIMENT 시트 전체를 DataFrame으로 반환.
    시트 없으면 빈 DataFrame 반환.
    """
    import pandas as pd
    try:
        ss = get_spreadsheet()
        try:
            sheet = ss.worksheet('SENTIMENT')
        except Exception:
            return pd.DataFrame(columns=SENTIMENT_HEADERS)

        records = sheet.get_all_records()
        if not records:
            return pd.DataFrame(columns=SENTIMENT_HEADERS)
        return pd.DataFrame(records)
    except Exception as e:
        print(f'[ERROR] get_sentiment: {e}')
        return pd.DataFrame(columns=SENTIMENT_HEADERS)


def archive_and_reset():
    """
    Midnight job:
    1. Move TODAY sheet -> per-ticker archive sheets
    2. Delete rows older than 90 days from each archive
    3. Clear TODAY sheet
    """
    ss = get_spreadsheet()
    cutoff = datetime.now(KST) - timedelta(days=90)

    try:
        today_sheet = ss.worksheet('TODAY')
    except gspread.WorksheetNotFound:
        print("TODAY sheet not found, skipping")
        return

    records = today_sheet.get_all_records(expected_headers=TODAY_HEADERS)
    if not records:
        today_sheet.clear()
        today_sheet.append_row(TODAY_HEADERS)
        return

    df = pd.DataFrame(records)
    df = df.fillna('')  # NaN(번역 미완료 셀 등) → '' 로 치환, JSON 직렬화 오류 방지

    def _sv(v):
        """float NaN/inf → '' (JSON 안전 이중 보호)"""
        import math
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return ''
        return v

    for ticker in df['ticker'].unique():
        ticker_df = df[df['ticker'] == ticker]
        archive = _ensure_sheet(ss, ticker, TODAY_HEADERS, 5000, 10)
        existing = get_existing_hashes(archive)
        new_rows = []
        for _, row in ticker_df.iterrows():
            if str(row['url_hash']) not in existing:
                new_rows.append([
                    _sv(row['ticker']),   _sv(row['company']),  _sv(row['title']),
                    _sv(row['link']),     _sv(row['published']), _sv(row['collected_at']),
                    _sv(row['url_hash']),
                    _sv(row.get('title_kr', '')),   _sv(row.get('summary_kr', '')),
                    _sv(row.get('article_summary_kr', ''))
                ])
        if new_rows:
            archive.append_rows(new_rows, value_input_option='RAW')

        # 헤더행 손상 방어: 빈/중복 헤더 시 자동 복구 후 재조회
        _first_row = archive.row_values(1) if archive.row_count > 0 else []
        if not _first_row or _first_row[:len(TODAY_HEADERS)] != TODAY_HEADERS:
            archive.clear()
            archive.append_row(TODAY_HEADERS)
            all_records = []
        else:
            all_records = archive.get_all_records(expected_headers=TODAY_HEADERS)
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
                rows = [
                    [r['ticker'], r['company'], r['title'],
                     r['link'], r['published'], r['collected_at'], r['url_hash'],
                     r.get('title_kr', ''), r.get('summary_kr', ''),
                     r.get('article_summary_kr', '')]
                    for r in keep
                ]
                archive.append_rows(rows, value_input_option='RAW')

    today_sheet.clear()
    today_sheet.append_row(TODAY_HEADERS)
    print("TODAY sheet reset complete")

def get_today_hashes_set() -> set:
    """TODAY 시트의 전체 url_hash set 반환 — 신규 기사 판별용."""
    try:
        ss = get_spreadsheet()
        try:
            today_sheet = ss.worksheet('TODAY')
        except gspread.WorksheetNotFound:
            return set()
        hashes = today_sheet.col_values(7)  # url_hash 컬럼 (7번째)
        return set(hashes[1:])              # 헤더 제외
    except Exception as e:
        print(f'[ERROR] get_today_hashes_set: {e}')
        return set()


def update_daily_summary(ticker: str, summary_kr: str) -> bool:
    """
    TODAY 시트에서 ticker 첫 번째 행의 summary_kr 셀을 갱신.
    일일 종합 브리핑(daily_briefing.py)에서 호출.
    """
    try:
        ss = get_spreadsheet()
        today_sheet = ss.worksheet('TODAY')
        all_values = today_sheet.get_all_values()
        headers = all_values[0] if all_values else []
        try:
            summary_col = headers.index('summary_kr') + 1  # 1-based
            ticker_col  = headers.index('ticker') + 1
        except ValueError:
            print(f'[ERROR] update_daily_summary: 헤더 컬럼 없음')
            return False

        for row_idx, row in enumerate(all_values[1:], start=2):
            if len(row) >= ticker_col and row[ticker_col - 1] == ticker.upper():
                today_sheet.update_cell(row_idx, summary_col, summary_kr)
                print(f'  [Sheets] {ticker} summary_kr 업데이트 완료 (row {row_idx})')
                return True

        print(f'  [Sheets] {ticker} TODAY 시트에 해당 행 없음 — 업데이트 스킵')
        return False
    except Exception as e:
        print(f'[ERROR] update_daily_summary({ticker}): {e}')
        return False


def get_latest_ticker_summary(ticker: str) -> dict:
    """
    티커 아카이브 시트에서 가장 최근 summary_kr 반환.
    오늘 TODAY 시트에 summary_kr이 없을 때 폴백용.
    반환: {'summary_kr': str, 'collected_at': str} or None
    """
    try:
        ss = get_spreadsheet()
        try:
            sheet = ss.worksheet(ticker.upper())
        except gspread.WorksheetNotFound:
            return None
        records = sheet.get_all_records()
        if not records:
            return None
        for row in reversed(records):
            sk = row.get('summary_kr', '')
            if sk and str(sk).strip():
                return {
                    'summary_kr': str(sk).strip(),
                    'collected_at': str(row.get('collected_at', ''))
                }
    except Exception as e:
        print(f'[ERROR] get_latest_ticker_summary({ticker}): {e}')
    return None
