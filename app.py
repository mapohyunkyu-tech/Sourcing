# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import date, timedelta
import pandas as pd
import streamlit as st

from database import (
    add_item, categories, category_map, get_item_names, get_photo_guide, load_analysis, load_database_df,
    save_analysis, search_items, save_raw_series, load_raw_series, cached_keywords,
    backup_database_bytes, restore_database_bytes, enqueue_keywords, next_pending_keywords,
    mark_queue_completed, mark_queue_failed, queue_status_df, queue_counts, reset_failed_queue,
    clear_pending_queue, log_api_call, api_log_df, today_api_calls,
    sync_queue_with_cache, initialize_database, reset_keywords_for_refresh,
    reset_categories_for_refresh, reset_all_collection_data, update_default_master_assets,
    save_season_signal, delete_season_signal, load_season_signals, apply_season_signals
)
from engine import ApiConfig, NaverApiError, analyze_keyword, call_api, completed_years
from settings_store import delete_credentials, load_settings, save_settings

APP_VERSION = "3.10.0-preemption-timeline"
st.set_page_config(page_title="MarketScout 시즌 AI v3", page_icon="📈", layout="wide")
st.title("📈 MarketScout 시즌 AI v3")
st.caption("공급처 탐색·사진 준비·선등록·광고 시작·피크를 한눈에 · 쿠팡 경쟁도 미사용")
settings = load_settings()

@st.cache_resource(show_spinner=False)
def init_once():
    return initialize_database()

try:
    init_once()
except Exception as e:
    st.error(f"DB 초기화 실패: {e}")
    st.stop()

@st.cache_data(show_spinner=False)
def db_df_cached(): return load_database_df()
@st.cache_data(show_spinner=False)
def category_map_cached(): return category_map()

db_df = db_df_cached(); products = category_map_cached()

def config_now():
    cid=settings.get('client_id','').strip(); sec=settings.get('client_secret','').strip()
    return ApiConfig(cid,sec,settings.get('auth_mode','developer')) if cid and sec else None
config=config_now()

def years_range():
    ys=completed_years(); return ys, f"{ys[0]}-01-01", f"{ys[-1]}-12-31"

def analyze_saved(keyword: str):
    ys,start,end=years_range(); s=load_raw_series(keyword,start,end)
    if s is None: return None
    raw=pd.DataFrame({keyword:s}); r=analyze_keyword(raw,keyword,date.today().year); save_analysis(r); return r


@st.cache_data(ttl=60, show_spinner=False)
def _dashboard_frame() -> pd.DataFrame:
    df = load_analysis(target_year=date.today().year)
    if df.empty:
        return df
    df = apply_season_signals(df, date.today().year)
    names = db_df[["대분류", "기준품목", "세부품목"]].drop_duplicates("세부품목")
    df = df.merge(names, how="left", left_on="search_keyword", right_on="세부품목")
    df["카테고리"] = df["대분류"].fillna("미분류")
    df["대표품목"] = df["기준품목"].fillna(df["search_keyword"])
    date_cols = [
        "exploration_start_date", "photo_prepare_date", "recommended_upload_date", "ad_start_date", "entry_date", "season_start_date",
        "expected_peak_start_date", "expected_peak_date",
        "expected_peak_end_date", "gentle_decline_start_date", "expected_end_date"
    ]
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    today = date.today()
    def stage(r):
        if r.get("season_type_calculated") == "사계절형": return "사계절"
        upload, entry, pstart, peak, pend, end = [r.get(k) for k in [
            "recommended_upload_date","entry_date","expected_peak_start_date",
            "expected_peak_date","expected_peak_end_date","expected_end_date"]]
        if pd.isna(entry) or pd.isna(end): return "분석대기"
        if today < upload: return "준비 전"
        if today < entry: return "등록·준비"
        if pstart and today < pstart: return "진입 초입"
        if peak and today <= peak: return "피크 상승"
        if pend and today <= pend: return "피크 구간"
        if today <= end: return "판매 후반"
        return "시즌 종료"
    df["단계"] = df.apply(stage, axis=1)
    df["탐색까지"] = df["exploration_start_date"].apply(lambda d: (d-today).days if pd.notna(d) else None)
    df["사진까지"] = df["photo_prepare_date"].apply(lambda d: (d-today).days if pd.notna(d) else None)
    df["등록까지"] = df["recommended_upload_date"].apply(lambda d: (d-today).days if pd.notna(d) else None)
    df["광고까지"] = df["ad_start_date"].apply(lambda d: (d-today).days if pd.notna(d) else None)
    df["진입까지"] = df["entry_date"].apply(lambda d: (d-today).days if pd.notna(d) else None)
    df["피크까지"] = df["expected_peak_date"].apply(lambda d: (d-today).days if pd.notna(d) else None)
    df["남은판매일"] = df["expected_end_date"].apply(lambda d: max(0,(d-today).days+1) if pd.notna(d) else None)
    return df

@st.cache_data(ttl=10, show_spinner=False)
def _queue_summary_cached():
    return queue_counts(), today_api_calls()

def clear_runtime_caches():
    _dashboard_frame.clear()
    _queue_summary_cached.clear()

def _fmt_date(v):
    return v.strftime("%m/%d") if pd.notna(v) else "-"

def _display_table(df: pd.DataFrame, columns: list[str], height: int = 330):
    if df.empty:
        st.info("해당 품목이 없습니다.")
        return
    view=df[columns].copy()
    rename={
        "search_keyword":"품목", "exploration_start_date":"탐색시작", "photo_prepare_date":"사진준비", "recommended_upload_date":"선등록", "ad_start_date":"광고시작", "entry_date":"진입일",
        "expected_peak_start_date":"피크시작", "expected_peak_date":"피크",
        "expected_peak_end_date":"피크종료", "expected_end_date":"판매종료",
        "season_type_confidence":"신뢰도", "judgement":"현재판단"
    }
    view=view.rename(columns=rename)
    for c in ["탐색시작","사진준비","선등록","광고시작","진입일","피크시작","피크","피크종료","판매종료"]:
        if c in view.columns: view[c]=view[c].apply(_fmt_date)
    st.dataframe(view, hide_index=True, use_container_width=True, height=height)



def _confidence_badge(v):
    try:
        score=float(v or 0)
    except Exception:
        score=0.0
    if score >= 90:
        return "🟢", "A", score
    if score >= 75:
        return "🟡", "B", score
    if score >= 60:
        return "🟠", "C", score
    return "🔴", "D", score

def _d_text(v, label):
    if v is None or pd.isna(v):
        return f"{label} -"
    try:
        n=int(v)
    except Exception:
        return f"{label} -"
    if n == 0:
        return f"{label} 오늘"
    if n > 0:
        return f"{label} D-{n}"
    return f"{label} D+{abs(n)}"

def _compact_items(df: pd.DataFrame, mode: str, limit: int = 12):
    if df.empty:
        st.caption("해당 품목 없음")
        return
    rows=[]
    for _,r in df.head(limit).iterrows():
        dot,grade,score=_confidence_badge(r.get("season_type_confidence"))
        name=str(r.get("search_keyword") or "-")
        if mode == "upload":
            detail=f"{_d_text(r.get('진입까지'),'진입')} · {_d_text(r.get('피크까지'),'피크')} · 판매 {int(r.get('남은판매일') or 0)}일"
        elif mode == "entry":
            detail=f"피크 {_d_text(r.get('피크까지'),'').replace('  ',' ').strip()} · 판매 {int(r.get('남은판매일') or 0)}일"
        elif mode == "selling":
            detail=f"{r.get('단계','-')} · 피크 {_fmt_date(r.get('expected_peak_date'))} · 판매 {int(r.get('남은판매일') or 0)}일"
        elif mode == "preparing":
            detail=f"{_d_text(r.get('진입까지'),'진입')} · 피크 {_fmt_date(r.get('expected_peak_date'))}"
        elif mode == "next30":
            detail=f"{_d_text(r.get('등록까지'),'등록')} · {_d_text(r.get('진입까지'),'진입')}"
        else:
            detail=f"등록 {_fmt_date(r.get('recommended_upload_date'))} · 진입 {_fmt_date(r.get('entry_date'))} · 피크 {_fmt_date(r.get('expected_peak_date'))}"
        rows.append(f"<div style='padding:7px 0;border-bottom:1px solid rgba(128,128,128,.18);font-size:0.94rem'><b>{dot} {name}</b> <span style='opacity:.72'>[{grade} {score:.0f}]</span><br><span style='opacity:.78'>{detail}</span></div>")
    st.markdown("".join(rows),unsafe_allow_html=True)
    if len(df)>limit:
        st.caption(f"외 {len(df)-limit:,}개 · 아래 상세표에서 전체 확인")

def result_card(r):
    st.subheader(f"{r['search_keyword']} 시즌 판단")
    c=st.columns(4)
    c[0].metric('판매유형',r.get('season_type_calculated','-'))
    c[1].metric('현재상태',r.get('judgement','-'))
    c[2].metric('신뢰도',f"{float(r.get('season_type_confidence') or 0):.0f}점")
    c[3].metric('남은 판매일',f"{int(r.get('remaining_sales_days') or 0)}일")
    st.success(r.get('recommended_action','-'))
    st.json({k:r.get(k) for k in ['exploration_start_date','photo_prepare_date','recommended_upload_date','ad_start_date','entry_date','expected_peak_start_date','expected_peak_date','expected_peak_end_date','expected_end_date','analysis_years']})

if config is None: st.warning("⚙️ 설정에서 API 키를 저장하세요. 저장된 품목 조회는 가능합니다.")

tabs=st.tabs(["📊 오늘 대시보드","🌱 출하 신호","🚚 대량 이어받기","🔍 품목 검색","🏠 저장 결과","🗂 DB 백업","⚙️ 설정"])

with tabs[0]:
    st.subheader("오늘의 판매 행동 대시보드")
    st.caption(f"기준일 {date.today().isoformat()} · 저장된 시즌 분석 결과 기준")
    dash = _dashboard_frame()
    if dash.empty:
        st.info("분석 결과가 아직 없습니다. 기존 DB를 복원하거나 대량 수집을 진행하세요.")
    else:
        cats=sorted(dash["카테고리"].dropna().unique().tolist())
        fc1,fc2=st.columns([3,1])
        selected_cats=fc1.multiselect("카테고리 필터",cats,default=cats)
        seasonal_only=fc2.checkbox("제철형만",value=True)
        view=dash[dash["카테고리"].isin(selected_cats)].copy()
        if seasonal_only: view=view[view["season_type_calculated"]=="제철형"]
        today=date.today()
        today_upload=view[view["recommended_upload_date"]==today]
        today_entry=view[view["entry_date"]==today]
        selling=view[(view["entry_date"].notna())&(view["expected_end_date"].notna())&(view["entry_date"]<=today)&(view["expected_end_date"]>=today)]
        preparing=view[(view["recommended_upload_date"].notna())&(view["entry_date"].notna())&(view["recommended_upload_date"]<=today)&(view["entry_date"]>today)]
        next30=view[(view["recommended_upload_date"].notna())&(view["등록까지"]>0)&(view["등록까지"]<=30)]
        evergreen=dash[dash["season_type_calculated"]=="사계절형"]
        m=st.columns(6)
        m[0].metric("오늘 등록",f"{len(today_upload):,}개")
        m[1].metric("오늘 진입",f"{len(today_entry):,}개")
        m[2].metric("현재 판매 중",f"{len(selling):,}개")
        m[3].metric("지금 준비 중",f"{len(preparing):,}개")
        m[4].metric("30일 내 준비",f"{len(next30):,}개")
        m[5].metric("사계절형",f"{len(evergreen):,}개")

        st.markdown("### ✅ 오늘 해야 할 일")
        action_order=["공급처 탐색","사진·상세페이지 준비","오늘 선등록","광고·가격 준비","판매 운영","아직 대기"]
        action_view=view[view["today_action"].isin(action_order)].sort_values(["preemption_score"],ascending=False)
        _display_table(action_view,["카테고리","search_keyword","today_action","preemption_score","year_adjust_days","exploration_start_date","photo_prepare_date","recommended_upload_date","ad_start_date","expected_peak_start_date","expected_peak_end_date"],420)

        st.markdown("#### 숫자 바로 아래 품목 요약")
        c1,c2=st.columns(2)
        with c1:
            st.markdown(f"##### 🔥 오늘 등록 {len(today_upload):,}개")
            _compact_items(today_upload.sort_values(["진입까지","expected_peak_date"]),"upload")
            st.markdown(f"##### 🚪 오늘 진입 {len(today_entry):,}개")
            _compact_items(today_entry.sort_values(["피크까지","expected_end_date"]),"entry")
            st.markdown(f"##### 🧰 지금 준비 중 {len(preparing):,}개")
            _compact_items(preparing.sort_values(["진입까지","expected_peak_date"]),"preparing")
        with c2:
            st.markdown(f"##### 🛒 현재 판매 중 {len(selling):,}개")
            _compact_items(selling.sort_values(["남은판매일","expected_peak_date"]),"selling")
            st.markdown(f"##### ⏰ 30일 내 준비 {len(next30):,}개")
            _compact_items(next30.sort_values(["등록까지","진입까지"]),"next30")

        low_conf=view[pd.to_numeric(view["season_type_confidence"],errors="coerce").fillna(0)<75].copy()
        st.markdown(f"### ⚠️ 검토 필요 · 신뢰도 B 미만 {len(low_conf):,}개")
        st.caption("🟢 A 90~100 · 🟡 B 75~89 · 🟠 C 60~74 · 🔴 D 60 미만")
        _compact_items(low_conf.sort_values(["season_type_confidence","recommended_upload_date"]),"review",limit=20)

        st.markdown("### 🔥 오늘 등록할 품목")
        _display_table(today_upload.sort_values(["진입까지","expected_peak_date"]),
            ["카테고리","search_keyword","진입까지","entry_date","expected_peak_start_date","expected_peak_date","expected_end_date","season_type_confidence"],280)

        st.markdown("### 🟢 현재 판매 중")
        sell_cols=["카테고리","search_keyword","단계","entry_date","expected_peak_start_date","expected_peak_date","expected_end_date","남은판매일","season_type_confidence"]
        _display_table(selling.sort_values(["남은판매일","expected_peak_date"]),sell_cols,380)

        left,right=st.columns(2)
        with left:
            st.markdown("### 🧰 지금 준비할 품목")
            _display_table(preparing.sort_values(["진입까지","expected_peak_date"]),
                ["카테고리","search_keyword","단계","진입까지","recommended_upload_date","entry_date","expected_peak_date","expected_end_date"],340)
        with right:
            st.markdown("### ⏰ 곧 준비 시작")
            _display_table(next30.sort_values(["등록까지","진입까지"]),
                ["카테고리","search_keyword","등록까지","진입까지","recommended_upload_date","entry_date","expected_peak_date"],340)

        peak_soon=view[(view["피크까지"].notna())&(view["피크까지"]>=0)&(view["피크까지"]<=21)]
        st.markdown("### 📈 21일 안에 피크가 오는 품목")
        _display_table(peak_soon.sort_values(["피크까지","남은판매일"]),
            ["카테고리","search_keyword","단계","피크까지","expected_peak_start_date","expected_peak_date","expected_peak_end_date","남은판매일"],320)

        with st.expander("전체 시즌 일정 보기",expanded=False):
            allv=view[view["season_type_calculated"]=="제철형"].sort_values(["recommended_upload_date","entry_date"])
            _display_table(allv,["카테고리","search_keyword","today_action","preemption_score","year_adjust_days","탐색까지","사진까지","등록까지","광고까지","진입까지","exploration_start_date","photo_prepare_date","recommended_upload_date","ad_start_date","entry_date","expected_peak_start_date","expected_peak_date","expected_peak_end_date","expected_end_date"],600)



with tabs[1]:
    st.subheader("올해 첫 수확·출하 신호")
    st.caption("확인한 출하 날짜를 저장하면 3년 평균 일정 전체를 최대 ±21일 보정합니다. 쿠팡 경쟁도는 계산하지 않습니다.")
    c1,c2=st.columns(2)
    signal_keyword=c1.selectbox("품목", sorted(db_df["세부품목"].dropna().astype(str).unique().tolist()), key="signal_keyword")
    signal_type=c2.selectbox("신호 종류",["첫 수확","첫 출하","본격 출하","공판장 반입"],key="signal_type")
    c3,c4=st.columns(2)
    signal_date=c3.date_input("확인 날짜",value=date.today(),key="signal_date")
    signal_note=c4.text_input("근거 메모",placeholder="예: 산지 농가 통화, 지역농협 첫 출하 기사",key="signal_note")
    a,b=st.columns(2)
    if a.button("출하 신호 저장",type="primary",use_container_width=True):
        save_season_signal(signal_keyword,date.today().year,signal_type,signal_date,signal_note); clear_runtime_caches(); st.success("저장했습니다. 대시보드 일정에 즉시 반영됩니다."); st.rerun()
    if b.button("선택 품목 신호 삭제",use_container_width=True):
        delete_season_signal(signal_keyword,date.today().year); clear_runtime_caches(); st.success("삭제했습니다."); st.rerun()
    sigdf=load_season_signals(date.today().year)
    if sigdf.empty: st.info("저장된 올해 출하 신호가 없습니다.")
    else:
        sigdf=sigdf.rename(columns={"search_keyword":"품목","signal_type":"신호","signal_date":"확인일","source_note":"근거","updated_at":"수정일"})
        st.dataframe(sigdf[["품목","신호","확인일","근거","수정일"]],hide_index=True,use_container_width=True)

with tabs[2]:
    st.subheader("대량 수집 관리자")
    st.info("한 번 실행할 분량만 처리하고 종료합니다. 자동 무한 반복하지 않습니다.")
    selected=st.multiselect("수집할 카테고리",list(products),default=[])
    col1,col2=st.columns(2)
    only_rep=col1.checkbox("대표품목만 수집",value=True,help="별칭마다 API를 쓰지 않고 대표품목만 수집합니다.")
    batch_calls=int(col2.number_input("이번 실행 API 호출 수",1,100,20,help="API 1회당 최대 5품목이므로 20회면 최대 100품목"))
    if st.button("선택 카테고리를 대기열에 넣기",disabled=not selected,use_container_width=True):
        rows=[]
        for cat in selected:
            part=db_df[db_df['대분류']==cat]
            if only_rep: part=part.drop_duplicates('기준품목').assign(세부품목=lambda x:x['기준품목'])
            rows += [(str(r['세부품목']),str(r['대분류']),str(r['기준품목'])) for _,r in part.iterrows()]
        enqueue_keywords(rows); clear_runtime_caches(); st.success(f"대기열에 {len(rows):,}개 반영")
    counts, today_calls = _queue_summary_cached(); m=st.columns(5)
    m[0].metric('전체',counts['total']);m[1].metric('완료',counts['completed']);m[2].metric('대기',counts['pending']);m[3].metric('실패',counts['failed']);m[4].metric('오늘 앱 호출',today_calls)
    a,b,c=st.columns(3)
    if a.button("실패 품목 다시 대기",use_container_width=True): reset_failed_queue(); clear_runtime_caches(); st.rerun()
    if b.button("캐시와 대기열 동기화",use_container_width=True):
        changed=sync_queue_with_cache(); clear_runtime_caches(); st.success(f"완료 상태 {changed:,}개 동기화"); st.rerun()
    if c.button("미완료 대기열 비우기",use_container_width=True): clear_pending_queue(); clear_runtime_caches(); st.rerun()

    with st.expander("🔄 새 데이터로 다시 받기", expanded=False):
        st.warning("재수집을 선택하면 해당 품목의 기존 3년 원자료와 분석 결과를 지우고 대기 상태로 돌립니다.")

        st.markdown("#### 특정 품목 다시 받기")
        refresh_text=st.text_area(
            "품목명 입력",
            placeholder="마늘쫑\n피자두\n샤인머스켓",
            help="줄바꿈 또는 쉼표로 여러 품목을 입력할 수 있습니다.",
            key="refresh_keywords_text"
        )
        parsed_refresh=[]
        for chunk in refresh_text.replace(',', '\n').splitlines():
            kw=chunk.strip()
            if kw and kw not in parsed_refresh: parsed_refresh.append(kw)
        if parsed_refresh:
            st.caption("재수집 대상: "+", ".join(parsed_refresh[:20])+(f" 외 {len(parsed_refresh)-20}개" if len(parsed_refresh)>20 else ""))
        if st.button("선택 품목을 새로 받기", disabled=not parsed_refresh, use_container_width=True):
            try:
                result=reset_keywords_for_refresh(parsed_refresh); clear_runtime_caches()
                st.success(f"{result['queued']:,}개를 재수집 대기로 변경했습니다. 기존 원자료 {result['cache_deleted']:,}개, 분석 {result['analysis_deleted']:,}개 삭제")
                st.session_state["refresh_keywords_text"]=""
                st.rerun()
            except Exception as e:
                st.error(f"재수집 초기화 실패: {e}")

        st.divider()
        st.markdown("#### 카테고리 전체 다시 받기")
        refresh_cats=st.multiselect("재수집할 카테고리", list(products), key="refresh_categories")
        refresh_rep_only=st.checkbox("대표품목만 다시 받기", value=True, key="refresh_rep_only")
        if st.button("선택 카테고리를 새로 받기", disabled=not refresh_cats, use_container_width=True):
            try:
                result=reset_categories_for_refresh(refresh_cats, refresh_rep_only); clear_runtime_caches()
                st.success(f"{result['queued']:,}개를 재수집 대기로 변경했습니다. 기존 원자료 {result['cache_deleted']:,}개, 분석 {result['analysis_deleted']:,}개 삭제")
                st.rerun()
            except Exception as e:
                st.error(f"카테고리 재수집 초기화 실패: {e}")

        st.divider()
        st.markdown("#### 전체 수집 데이터 처음부터 다시 받기")
        st.error("이 작업은 저장된 모든 3년 원자료와 시즌 분석 결과를 삭제합니다. 품목 마스터는 유지됩니다.")
        if st.button("전체 초기화용 DB 백업 만들기", use_container_width=True, key="refresh_backup_prepare"):
            with st.spinner("안전 백업을 만드는 중입니다..."):
                st.session_state["refresh_backup_bytes"]=backup_database_bytes()
                st.session_state["refresh_backup_name"]=f"MarketScout_before_reset_{date.today().isoformat()}.db"
        if st.session_state.get("refresh_backup_bytes"):
            st.download_button(
                "① 초기화 전 DB 백업 다운로드",
                st.session_state["refresh_backup_bytes"],
                file_name=st.session_state.get("refresh_backup_name","MarketScout_before_reset.db"),
                use_container_width=True, key="refresh_backup_download"
            )
            confirm_reset=st.text_input("② 확인문구 입력: 전체초기화", key="confirm_full_reset")
            confirm_check=st.checkbox("백업 파일을 내려받았고 기존 수집 데이터 삭제에 동의합니다.", key="confirm_full_reset_check")
            if st.button(
                "③ 모든 수집 데이터 삭제 후 전체 대기 전환",
                type="primary",
                disabled=confirm_reset.strip()!="전체초기화" or not confirm_check,
                use_container_width=True
            ):
                try:
                    result=reset_all_collection_data(); clear_runtime_caches()
                    st.session_state.pop("refresh_backup_bytes",None)
                    st.success(f"전체 초기화 완료: 원자료 {result['cache_deleted']:,}개, 분석 {result['analysis_deleted']:,}개 삭제 · 대기열 {result['queued']:,}개 재수집 대기")
                    st.rerun()
                except Exception as e:
                    st.error(f"전체 초기화 실패: {e}")

    if st.button("▶ 이어받기 시작",type="primary",disabled=config is None or counts['pending']==0,use_container_width=True):
        ys,start,end=years_range(); progress=st.progress(0); status=st.empty(); done=0; stopped=False
        max_items=batch_calls*5
        for call_no in range(batch_calls):
            pending=next_pending_keywords(5)
            if pending.empty: break
            keywords=pending['keyword'].tolist()
            status.write(f"API {call_no+1}/{batch_calls} · {', '.join(keywords)}")
            try:
                data=call_api(config,keywords,start,end,retries=1)
                log_api_call(keywords,'success',f"returned={list(data.keys())}")
                for kw in keywords:
                    if kw in data and not data[kw].dropna().empty:
                        save_raw_series(kw,data[kw],start,end)
                        mark_queue_completed(kw)
                        try: analyze_saved(kw)
                        except Exception: pass
                        done+=1
                    else:
                        mark_queue_failed(kw,'데이터 없음')
                progress.progress(min(1.0,done/max(1,max_items)))
            except NaverApiError as e:
                log_api_call(keywords,'error',str(e))
                msg=str(e)
                if '429' in msg or 'Query limit exceeded' in msg:
                    st.error("API 한도 초과로 즉시 중단했습니다. 지금까지 완료된 품목은 모두 저장되어 있습니다. 새 키 저장 후 다시 이어받기를 누르세요.")
                    stopped=True; break
                for kw in keywords: mark_queue_failed(kw,msg)
                st.error(msg); stopped=True; break
            except Exception as e:
                log_api_call(keywords,'error',str(e))
                for kw in keywords: mark_queue_failed(kw,str(e))
                st.error(f"수집 중 오류: {e}"); stopped=True; break
        clear_runtime_caches()
        if not stopped: st.success(f"이번 실행 완료: 신규 저장 {done}개")
        st.caption("페이지를 새로고침하면 최신 진행 현황이 표시됩니다.")

    with st.expander("대기열 상세 보기", expanded=False):
        st.caption("상세 목록은 이 영역을 열었을 때만 불러옵니다.")
        if st.button("대기열 목록 불러오기", key="load_queue"):
            st.session_state["show_queue"] = True
        if st.session_state.get("show_queue"):
            qdf=queue_status_df()
            if not qdf.empty:
                filt=st.selectbox('대기열 보기',['전체','pending','completed','failed'])
                view=qdf if filt=='전체' else qdf[qdf['상태']==filt]
                st.dataframe(view.head(1000),hide_index=True,use_container_width=True,height=420)
                if len(view)>1000: st.caption(f"화면에는 처음 1,000개만 표시합니다. 전체 {len(view):,}개")
            else:
                st.info("대기열이 비어 있습니다.")
    with st.expander("API 호출 로그", expanded=False):
        if st.button("호출 로그 불러오기", key="load_logs"):
            st.session_state["show_logs"] = True
        if st.session_state.get("show_logs"):
            st.dataframe(api_log_df(200),hide_index=True,use_container_width=True)

with tabs[3]:
    q=st.text_input("품목명",placeholder="마늘쫑")
    matches=search_items(q) if q.strip() else pd.DataFrame()
    if q.strip() and matches.empty:
        st.warning("DB에 없는 품목")
        with st.expander("새 품목 추가",expanded=True):
            cat=st.selectbox('카테고리',categories()); rep=st.text_input('대표품목',value=q.strip())
            if st.button('추가'):
                add_item(cat,rep,q.strip(),'미정','별칭' if rep!=q.strip() else '대표명');db_df_cached.clear();category_map_cached.clear();st.rerun()
    elif not matches.empty:
        idx=st.selectbox('검색 결과',matches.index,format_func=lambda i:f"{matches.loc[i,'검색명']} · 대표 {matches.loc[i,'대표품목']} · {matches.loc[i,'카테고리']}")
        row=matches.loc[idx]; kw=str(row['검색명']); rep=str(row['대표품목'])
        st.success(f"DB 등록됨 · 대표품목 {rep}")
        st.caption('연결명: '+', '.join(get_item_names(rep)[:50]))

        guide=get_photo_guide(kw,rep)
        st.markdown("### 📷 사진 활용 가이드")
        g1,g2=st.columns(2)
        with g1:
            st.markdown(f"**같은 품종·동일 유통명**  \n{guide.get('same_variety_names') or '등록 정보 없음'}")
            st.markdown(f"**겉모양 유사 품종**  \n{guide.get('similar_outer_varieties') or '확인 필요'}")
            st.markdown(f"**단면 유사 품종**  \n{guide.get('similar_cut_varieties') or '확인 필요'}")
        with g2:
            st.markdown(f"**겉색**  \n{guide.get('skin_color') or '확인 필요'}")
            st.markdown(f"**과육색**  \n{guide.get('flesh_color') or '확인 필요'}")
            allowed=guide.get('substitute_photo_allowed') or '조건부 가능'
            if allowed == '가능': st.success(f"사진 대체 사용: {allowed}")
            elif allowed == '불가': st.error(f"사진 대체 사용: {allowed}")
            else: st.warning(f"사진 대체 사용: {allowed}")
        st.info(guide.get('substitute_photo_notes') or '실제 판매 품목과 외형·단면·색상이 일치하는지 확인하세요.')
        st.caption("※ 대체 사진은 형태 설명용 가이드입니다. 실제 판매 상품 사진으로 오인되지 않도록 상세페이지에 품종·산지·실물 차이를 명확히 표시하세요.")

        ys,start,end=years_range(); saved=load_raw_series(kw,start,end)
        if saved is None and kw!=rep: saved=load_raw_series(rep,start,end)
        c1,c2=st.columns(2)
        if c1.button('저장 데이터로 분석',disabled=saved is None,use_container_width=True):
            use_kw=kw if load_raw_series(kw,start,end) is not None else rep
            r=analyze_saved(use_kw); st.session_state['one_result']=r
        if c2.button('API로 1회 새 조회',disabled=config is None,use_container_width=True):
            try:
                data=call_api(config,[kw],start,end,retries=1);log_api_call([kw],'success')
                if kw not in data: raise NaverApiError('데이터 없음')
                save_raw_series(kw,data[kw],start,end);mark_queue_completed(kw)
                st.session_state['one_result']=analyze_saved(kw)
            except Exception as e: log_api_call([kw],'error',str(e));st.error(str(e))
        r=st.session_state.get('one_result')
        if r: result_card(r)

with tabs[4]:
    saved=load_analysis(target_year=date.today().year)
    st.dataframe(saved,hide_index=True,use_container_width=True,height=650) if not saved.empty else st.info('저장 결과 없음')

with tabs[5]:
    st.subheader("DB 백업 및 복원")
    if st.button("저장 품목 목록 불러오기", key="load_cache_list", use_container_width=True):
        st.session_state["show_cache_list"] = True
    if st.session_state.get("show_cache_list"):
        cache=cached_keywords(); st.metric('3년 원자료 저장 품목',len(cache))
        st.dataframe(cache.head(1000),hide_index=True,use_container_width=True,height=300)
        if len(cache)>1000: st.caption(f"화면에는 최근 1,000개만 표시합니다. 전체 {len(cache):,}개")
    else:
        st.info("목록과 백업 파일은 필요할 때만 생성하므로 평소 화면이 빠르게 열립니다.")

    if st.button("백업 파일 준비", key="prepare_backup", use_container_width=True):
        with st.spinner("DB 백업 파일을 만드는 중입니다..."):
            st.session_state["db_backup_bytes"] = backup_database_bytes()
            st.session_state["db_backup_name"] = f"MarketScout_v3_{date.today().isoformat()}.db"
    if st.session_state.get("db_backup_bytes"):
        st.download_button('DB 백업 다운로드',st.session_state["db_backup_bytes"],file_name=st.session_state.get("db_backup_name","MarketScout_v3.db"),use_container_width=True)
    up=st.file_uploader('DB 복원',type=['db','sqlite','sqlite3'])
    if up and st.button('복원 실행',type='primary'):
        try: restore_database_bytes(up.getvalue());db_df_cached.clear();category_map_cached.clear();clear_runtime_caches();st.success('복원 완료');st.rerun()
        except Exception as e: st.error(str(e))
    st.warning('Streamlit Cloud 재배포 시 로컬 DB가 초기화될 수 있으니 수집 후 백업하세요.')

    st.divider()
    st.subheader("⚙️ 기본 DB 업데이트")
    st.caption("현재 DB의 활성 품목과 사진 활용 가이드를 item_master.csv에 병합하고, 분석 데이터가 비어 있는 새 설치용 default_market_scout.db를 생성합니다.")
    st.info("로컬 실행에서는 프로젝트 파일을 즉시 교체합니다. Streamlit Cloud에서는 배포 파일 변경이 재배포 후 사라질 수 있으므로 생성된 두 파일을 내려받아 GitHub 프로젝트에도 교체하세요.")
    confirm_master=st.checkbox("현재 품목을 기본 마스터에 반영합니다.", key="confirm_default_master_update")
    if st.button("기본 DB 업데이트 실행", type="primary", disabled=not confirm_master, use_container_width=True, key="run_default_master_update"):
        try:
            with st.spinner("현재 품목을 병합하고 새 기본 DB를 만드는 중입니다..."):
                result=update_default_master_assets()
                st.session_state["master_update_result"]=result
            clear_runtime_caches(); db_df_cached.clear(); category_map_cached.clear()
            where=[]
            if result.get("csv_written"): where.append("item_master.csv 교체")
            if result.get("default_db_written"): where.append("기본 DB 교체")
            suffix=" · ".join(where) if where else "다운로드 파일 생성"
            st.success(f"완료: 활성 품목 {result['item_count']:,}개 · CSV 신규 병합 {result['added_to_csv']:,}개 · {suffix}")
        except Exception as e:
            st.error(f"기본 DB 업데이트 실패: {e}")
    master_result=st.session_state.get("master_update_result")
    if master_result:
        d1,d2=st.columns(2)
        d1.download_button(
            "갱신된 item_master.csv 다운로드", master_result["csv_bytes"],
            file_name="item_master.csv", mime="text/csv", use_container_width=True,
            key="download_updated_master_csv"
        )
        d2.download_button(
            "갱신된 기본 DB 다운로드", master_result["default_db_bytes"],
            file_name="default_market_scout.db", mime="application/octet-stream",
            use_container_width=True, key="download_updated_default_db"
        )

with tabs[6]:
    modes={'developer':'NAVER Developers 데이터랩','hub':'NAVER API HUB','legacy_ncp':'NAVER Cloud 기존 방식'}
    mode=st.selectbox('인증 방식',list(modes),format_func=lambda x:modes[x],index=list(modes).index(settings.get('auth_mode','developer')) if settings.get('auth_mode','developer') in modes else 0)
    cid=st.text_input('Client ID',value=settings.get('client_id',''));sec=st.text_input('Client Secret',value=settings.get('client_secret',''),type='password')
    c=st.columns(3)
    if c[0].button('저장',type='primary',use_container_width=True): settings.update({'auth_mode':mode,'client_id':cid.strip(),'client_secret':sec.strip()});save_settings(settings);st.rerun()
    if c[1].button('연결 테스트',use_container_width=True):
        try: call_api(ApiConfig(cid.strip(),sec.strip(),mode),['사과'],(date.today()-timedelta(days=30)).isoformat(),date.today().isoformat(),retries=1);st.success('연결 성공')
        except Exception as e: st.error(str(e))
    if c[2].button('키 삭제',use_container_width=True): delete_credentials();st.rerun()
    st.caption(f"앱 버전 {APP_VERSION}")
