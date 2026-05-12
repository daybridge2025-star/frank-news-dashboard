"""
Frank News Dashboard — Streamlit 웹 대시보드
Google Sheets(TODAY 시트)에서 뉴스를 읽어 종목별로 표시
"""

import streamlit as st
import pandas as pd
from datetime import datetime
import pytz
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from utils.sheets import get_tickers, add_ticker, remove_ticker, get_today_news

KST = pytz.timezone('Asia/Seoul')

# ── 페이지 기본 설정 ─────────────────────────────────────────────
st.set_page_config(
    page_title="Frank News Dashboard",
    page_icon="📰",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ──────────────────────────────────────────────────────────
st.markdown("""
<style>
    .news-card {
        background: #1e1e2e;
        border: 1px solid #313244;
        border-radius: 10px;
        padding: 14px 18px;
        margin-bottom: 10px;
        line-height: 1.6;
    }
    .news-card a {
        color: #89dceb;
        text-decoration: none;
        font-weight: 600;
        font-size: 0.95rem;
    }
    .news-card a:hover { text-decoration: underline; }
    .news-meta {
        color: #6c7086;
        font-size: 0.78rem;
        margin-top: 4px;
    }
    .ticker-badge {
        display: inline-block;
        background: #313244;
        color: #cdd6f4;
        border-radius: 6px;
        padding: 2px 10px;
        font-size: 0.8rem;
        font-weight: 700;
        margin-right: 6px;
    }
    .section-header {
        font-size: 1.25rem;
        font-weight: 700;
        color: #cdd6f4;
        margin: 18px 0 10px 0;
        border-left: 4px solid #89b4fa;
        padding-left: 10px;
    }
    .page-btn-row {
        display: flex;
        gap: 8px;
        margin-top: 6px;
    }
    .stButton>button {
        border-radius: 8px;
    }
</style>
""", unsafe_allow_html=True)


# ── 유틸 ─────────────────────────────────────────────────────────
def kst_now_str():
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')


@st.cache_data(ttl=300)
def load_news():
    """5분 캐시로 TODAY 시트 읽기"""
    return get_today_news()


@st.cache_data(ttl=300)
def load_tickers():
    return get_tickers()


def clear_cache():
    load_news.clear()
    load_tickers.clear()


# ── 사이드바 ─────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ 종목 관리")
    st.caption(f"업데이트: {kst_now_str()}")

    if st.button("🔄 새로고침", use_container_width=True):
        clear_cache()
        st.rerun()

    st.divider()

    # 종목 추가
    st.subheader("➕ 종목 추가")
    with st.form("add_ticker_form", clear_on_submit=True):
        new_ticker = st.text_input("티커 (예: AAPL)", max_chars=10).upper().strip()
        new_company = st.text_input("회사명 (예: 애플)", max_chars=50).strip()
        submitted = st.form_submit_button("추가", use_container_width=True)
        if submitted:
            if new_ticker and new_company:
                try:
                    add_ticker(new_ticker, new_company)
                    clear_cache()
                    st.success(f"{new_ticker} 추가 완료!")
                    st.rerun()
                except Exception as e:
                    st.error(f"추가 실패: {e}")
            else:
                st.warning("티커와 회사명을 모두 입력해주세요.")

    st.divider()

    # 종목 삭제
    st.subheader("🗑️ 종목 삭제")
    tickers_raw = load_tickers()
    if tickers_raw:
        ticker_options = {f"{t['ticker']} ({t['company_name']})": t['ticker'] for t in tickers_raw}
        selected_label = st.selectbox("삭제할 종목 선택", list(ticker_options.keys()))
        if st.button("삭제", use_container_width=True, type="secondary"):
            try:
                remove_ticker(ticker_options[selected_label])
                clear_cache()
                st.success(f"{ticker_options[selected_label]} 삭제 완료!")
                st.rerun()
            except Exception as e:
                st.error(f"삭제 실패: {e}")
    else:
        st.info("등록된 종목이 없습니다.")


# ── 메인 ─────────────────────────────────────────────────────────
st.title("📰 Frank News Dashboard")
st.caption("미국 주식 뉴스 자동 수집 대시보드 | 매시 정각 업데이트")
st.divider()

df = load_news()
tickers = load_tickers()

if df.empty or tickers is None:
    st.info("📭 아직 수집된 뉴스가 없습니다. GitHub Actions가 매시 정각에 뉴스를 수집합니다.")
    st.stop()

# 등록 종목 순서대로 섹션 배치
ticker_order = [t['ticker'] for t in tickers]

# 세션 스테이트: 종목별 페이지 번호
if 'page' not in st.session_state:
    st.session_state['page'] = {}

ITEMS_PER_PAGE = 10  # 페이지당 10건
PREVIEW_COUNT = 10   # 첫 페이지 표시 건수

found_any = False

for ticker_sym in ticker_order:
    ticker_df = df[df['ticker'] == ticker_sym].copy()
    if ticker_df.empty:
        continue

    found_any = True

    # 수집 시각 기준 최신순 정렬
    if 'collected_at' in ticker_df.columns:
        ticker_df = ticker_df.sort_values('collected_at', ascending=False)

    company_name = ticker_df['company'].iloc[0] if 'company' in ticker_df.columns else ticker_sym
    total = len(ticker_df)

    # 페이지 번호 초기화
    page_key = f"page_{ticker_sym}"
    if page_key not in st.session_state['page']:
        st.session_state['page'][page_key] = 0

    current_page = st.session_state['page'][page_key]
    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    # 섹션 헤더
    st.markdown(
        f'<div class="section-header">'
        f'<span class="ticker-badge">{ticker_sym}</span>{company_name} '
        f'<span style="color:#6c7086;font-size:0.85rem;font-weight:400;">({total}건)</span>'
        f'</div>',
        unsafe_allow_html=True
    )

    # 현재 페이지 슬라이스
    start = current_page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_df = ticker_df.iloc[start:end]

    for idx, (_, row) in enumerate(page_df.iterrows(), start=start + 1):
        title = row.get('title', '(제목 없음)')
        link = row.get('link', '#')
        published = row.get('published', '')
        collected = row.get('collected_at', '')

        meta_parts = []
        if published:
            meta_parts.append(f"📅 {published[:25]}")
        if collected:
            meta_parts.append(f"수집: {collected[:16]}")
        meta = " &nbsp;|&nbsp; ".join(meta_parts)

        st.markdown(
            f'<div class="news-card">'
            f'<span style="color:#6c7086;font-size:0.8rem;">#{idx}</span> '
            f'<a href="{link}" target="_blank">{title}</a>'
            f'<div class="news-meta">{meta}</div>'
            f'</div>',
            unsafe_allow_html=True
        )

    # 페이지네이션 버튼 (2페이지 이상일 때만)
    if total_pages > 1:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col1:
            if current_page > 0:
                if st.button("◀ 이전", key=f"prev_{ticker_sym}"):
                    st.session_state['page'][page_key] -= 1
                    st.rerun()
        with col2:
            st.markdown(
                f'<div style="text-align:center;color:#6c7086;font-size:0.85rem;padding-top:8px;">'
                f'{current_page + 1} / {total_pages} 페이지</div>',
                unsafe_allow_html=True
            )
        with col3:
            if current_page < total_pages - 1:
                if st.button("다음 ▶", key=f"next_{ticker_sym}"):
                    st.session_state['page'][page_key] += 1
                    st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

if not found_any:
    st.info("📭 등록된 종목에 해당하는 뉴스가 없습니다. 잠시 후 다시 확인해주세요.")

# ── 푸터 ─────────────────────────────────────────────────────────
st.divider()
st.caption("Frank News Dashboard · Google News RSS 기반 · 매시 정각 자동 수집 · GitHub Actions")
