# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import date, timedelta
import pandas as pd
import streamlit as st

from database import add_item, categories, category_map, get_item_names, load_analysis, load_database_df, save_analysis, search_items
from engine import ApiConfig, analyze, analyze_keyword, call_api, collect, completed_years, to_excel
from settings_store import delete_credentials, load_settings, save_settings

st.set_page_config(page_title="MarketScout 시즌 AI", page_icon="📈", layout="wide")
st.title("📈 MarketScout 시즌 AI")
st.caption("품목 검색 → 최근 완료 3년 분석 → 진입일·피크일·후반 판매기간·종료일 판단")
settings=load_settings(); db_df=load_database_df(); products=category_map()

def config_now():
    if settings.get("client_id") and settings.get("client_secret"):
        return ApiConfig(settings["client_id"],settings["client_secret"],settings.get("auth_mode","developer"))
    return None
config=config_now()
for k,v in {"quick_result":None,"raw":None,"results":None}.items():st.session_state.setdefault(k,v)

def fd(v):
    return pd.to_datetime(v).strftime("%m/%d") if v else "-"

def result_card(r):
    st.markdown(f"### {r['search_keyword']} 시즌 판단")
    a,b,c,d=st.columns(4)
    a.metric("판매유형",r["season_type_calculated"])
    b.metric("현재상태",r.get("judgement","-"))
    c.metric("신뢰도",f"{float(r.get('season_type_confidence') or 0):.0f}점")
    d.metric("남은 판매일",f"{int(r.get('remaining_sales_days') or 0)}일" if r["season_type_calculated"]!="사계절형" else "상시")
    if r["season_type_calculated"]=="사계절형":
        st.info(f"사계절 판매 가능 · 최근 30일 변화 {r.get('recent_30d_change','-')}%")
        return
    cols=st.columns(6)
    for col,label,key in zip(cols,["등록 준비","진입","시즌 시작","예상 피크","피크구간","종료"],["recommended_upload_date","entry_date","season_start_date","expected_peak_date","expected_peak_start_date","expected_end_date"]):
        if key=="expected_peak_start_date": col.metric(label,f"{fd(r.get(key))}~{fd(r.get('expected_peak_end_date'))}")
        else: col.metric(label,fd(r.get(key)))
    st.success(f"추천 행동: {r.get('recommended_action','-')}")
    st.caption(f"연도별 피크: {r.get('yearly_peak_dates','-')} · 시즌 진행률 {r.get('season_progress',0)}% · 분석연도 {r.get('analysis_years','-')}")

tabs=st.tabs(["🔍 품목 즉시판단","📆 월별 분석","🏠 저장 결과","🗂 DB","⚙️ 설정"])
with tabs[0]:
    q=st.text_input("업체에서 제안받은 품목명",placeholder="예: 마늘쫑, 마늘종, 아오리사과")
    matches=search_items(q) if q.strip() else pd.DataFrame()
    if q.strip() and matches.empty:
        st.warning("DB에 없는 품목입니다.")
        with st.expander("새 품목 추가",expanded=True):
            c1,c2,c3=st.columns(3); cat=c1.selectbox("카테고리",categories()); rep=c2.text_input("대표품목",value=q.strip()); typ=c3.selectbox("유형",["미정","제철형","사계절형"])
            if st.button("추가 후 3년 분석",type="primary",disabled=config is None):
                add_item(cat,rep,q.strip(),typ,"유통명" if rep!=q.strip() else "대표 원물명"); st.rerun()
    elif not matches.empty:
        idx=st.selectbox("검색 결과",matches.index,format_func=lambda i:f"{matches.loc[i,'검색명']} · 대표 {matches.loc[i,'대표품목']} · {matches.loc[i,'카테고리']} · {matches.loc[i,'이름유형']}")
        s=matches.loc[idx]; st.success(f"DB 등록됨 · 대표품목 {s['대표품목']} · {s['카테고리']}")
        aliases=get_item_names(str(s["대표품목"])); st.caption("연결 품목명: "+", ".join(aliases[:40])+(" …" if len(aliases)>40 else ""))
        cached=load_analysis(str(s["검색명"]),date.today().year)
        c1,c2=st.columns(2)
        if c1.button("최근 완료 3년 새로 분석",type="primary",disabled=config is None,use_container_width=True):
            ys=completed_years()
            with st.spinner(f"{ys[0]}~{ys[-1]} 데이터 분석 중"):
                raw=collect(config,[str(s['검색명'])],f"{ys[0]}-01-01",f"{ys[-1]}-12-31")
                r=analyze_keyword(raw,str(s['검색명']),date.today().year);save_analysis(r);st.session_state.quick_result=r;st.session_state.raw=raw
        if c2.button("저장 결과 보기",disabled=cached.empty,use_container_width=True):st.session_state.quick_result=cached.iloc[0].to_dict()
        r=st.session_state.quick_result
        if r and r.get("search_keyword")==str(s["검색명"]):
            st.divider();result_card(r)
            raw=st.session_state.raw
            if raw is not None and str(s["검색명"]) in raw.columns:st.line_chart(raw[[str(s["검색명"])]])
with tabs[1]:
    a,b,c=st.columns(3);year=int(a.number_input("적용 연도",date.today().year,2035,date.today().year));month=int(b.selectbox("월",range(1,13),index=date.today().month-1,format_func=lambda x:f"{x}월"));cats=c.multiselect("카테고리",list(products),default=list(products)[:1])
    if st.button("월별 분석 실행",type="primary",disabled=config is None or not cats,use_container_width=True):
        items=[x for cat in cats for x in products[cat]];ys=completed_years();bar=st.progress(0)
        def prog(n,total,batch):bar.progress(n/total)
        raw=collect(config,items,f"{ys[0]}-01-01",f"{ys[-1]}-12-31",prog);res=analyze(raw,{cat:products[cat] for cat in cats},year,month);st.session_state.results=res;st.session_state.raw=raw
    res=st.session_state.results
    if res is not None:
        if res.empty:st.info("해당 월 결과가 없습니다.")
        else:
            show=["카테고리","품목","season_type_calculated","judgement","recommended_upload_date","entry_date","expected_peak_date","expected_peak_end_date","expected_end_date","remaining_sales_days","recommended_action","season_type_confidence"]
            st.dataframe(res[[x for x in show if x in res]],hide_index=True,use_container_width=True,height=620)
            st.download_button("Excel 다운로드",to_excel(res,st.session_state.raw),file_name=f"MarketScout_{year}_{month:02d}.xlsx")
with tabs[2]:
    cached=load_analysis(target_year=date.today().year)
    if cached.empty:st.info("저장된 분석이 없습니다.")
    else:st.dataframe(cached,hide_index=True,use_container_width=True,height=650)
with tabs[3]:
    st.success(f"검색 가능 품목명 {len(db_df):,}개 · 대표품목 {db_df['기준품목'].nunique():,}개")
    find=st.text_input("DB 검색");view=db_df[db_df["세부품목"].str.contains(find,case=False,na=False)] if find else db_df
    st.dataframe(view,hide_index=True,use_container_width=True,height=650)
with tabs[4]:
    modes={"developer":"NAVER Developers 데이터랩","hub":"NAVER API HUB","legacy_ncp":"NAVER Cloud 기존 방식"};mode=st.selectbox("인증 방식",list(modes),format_func=lambda x:modes[x],index=list(modes).index(settings.get("auth_mode","developer")) if settings.get("auth_mode","developer") in modes else 0)
    cid=st.text_input("Client ID",value=settings.get("client_id",""));secret=st.text_input("Client Secret",value=settings.get("client_secret",""),type="password")
    a,b,c=st.columns(3)
    if a.button("저장",type="primary",use_container_width=True):settings.update({"auth_mode":mode,"client_id":cid.strip(),"client_secret":secret.strip()});save_settings(settings);st.rerun()
    if b.button("연결 테스트",use_container_width=True):
        try:call_api(ApiConfig(cid.strip(),secret.strip(),mode),["사과"],(date.today()-timedelta(days=30)).isoformat(),date.today().isoformat(),1);st.success("API 연결 성공")
        except Exception as e:st.error(str(e))
    if c.button("저장 키 삭제",use_container_width=True):delete_credentials();st.rerun()
