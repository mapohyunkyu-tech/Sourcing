# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import date, timedelta
import pandas as pd
import streamlit as st

from database import (
    add_item, categories, category_map, get_item_names, load_analysis, load_database_df,
    save_analysis, search_items, save_raw_series, load_raw_series, cached_keywords,
    backup_database_bytes, restore_database_bytes, enqueue_keywords, next_pending_keywords,
    mark_queue_completed, mark_queue_failed, queue_status_df, queue_counts, reset_failed_queue,
    clear_pending_queue, log_api_call, api_log_df, today_api_calls,
    sync_queue_with_cache, initialize_database
)
from engine import ApiConfig, NaverApiError, analyze_keyword, call_api, completed_years
from settings_store import delete_credentials, load_settings, save_settings

APP_VERSION = "3.3.0-no-rerun-ddl"
st.set_page_config(page_title="MarketScout 시즌 AI v3", page_icon="📈", layout="wide")
st.title("📈 MarketScout 시즌 AI v3")
st.caption("DB 초기화 1회 · 평상시 읽기 전용 · 5품목 묶음 수집 · 이어받기")
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

def result_card(r):
    st.subheader(f"{r['search_keyword']} 시즌 판단")
    c=st.columns(4)
    c[0].metric('판매유형',r.get('season_type_calculated','-'))
    c[1].metric('현재상태',r.get('judgement','-'))
    c[2].metric('신뢰도',f"{float(r.get('season_type_confidence') or 0):.0f}점")
    c[3].metric('남은 판매일',f"{int(r.get('remaining_sales_days') or 0)}일")
    st.success(r.get('recommended_action','-'))
    st.json({k:r.get(k) for k in ['recommended_upload_date','entry_date','season_start_date','expected_peak_start_date','expected_peak_date','expected_peak_end_date','expected_end_date','analysis_years']})

if config is None: st.warning("⚙️ 설정에서 API 키를 저장하세요. 저장된 품목 조회는 가능합니다.")

tabs=st.tabs(["🚚 대량 이어받기","🔍 품목 검색","🏠 저장 결과","🗂 DB 백업","⚙️ 설정"])

with tabs[0]:
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
        enqueue_keywords(rows); st.success(f"대기열에 {len(rows):,}개 반영")
    counts=queue_counts(); m=st.columns(5)
    m[0].metric('전체',counts['total']);m[1].metric('완료',counts['completed']);m[2].metric('대기',counts['pending']);m[3].metric('실패',counts['failed']);m[4].metric('오늘 앱 호출',today_api_calls())
    a,b,c=st.columns(3)
    if a.button("실패 품목 다시 대기",use_container_width=True): reset_failed_queue(); st.rerun()
    if b.button("캐시와 대기열 동기화",use_container_width=True):
        changed=sync_queue_with_cache(); st.success(f"완료 상태 {changed:,}개 동기화"); st.rerun()
    if c.button("미완료 대기열 비우기",use_container_width=True): clear_pending_queue(); st.rerun()

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

with tabs[1]:
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

with tabs[2]:
    saved=load_analysis(target_year=date.today().year)
    st.dataframe(saved,hide_index=True,use_container_width=True,height=650) if not saved.empty else st.info('저장 결과 없음')

with tabs[3]:
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
        try: restore_database_bytes(up.getvalue());db_df_cached.clear();category_map_cached.clear();st.success('복원 완료');st.rerun()
        except Exception as e: st.error(str(e))
    st.warning('Streamlit Cloud 재배포 시 로컬 DB가 초기화될 수 있으니 수집 후 백업하세요.')

with tabs[4]:
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
