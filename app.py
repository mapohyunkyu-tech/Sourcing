# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import date, timedelta
import pandas as pd
import streamlit as st

try:
    from database import (
        add_item, add_product_form, add_search_rule, categories, category_map, get_item_details,
        get_item_names, keyword_plan, load_analysis, load_database_df, product_forms,
        save_analysis, search_items, validate_database,
    )
    from engine import (
        ApiConfig, NaverApiError, analyze, analyze_keyword, call_api, collect,
        collect_plans, completed_years, to_excel,
    )
except ImportError as exc:
    st.error(
        "프로그램 파일 버전이 서로 다릅니다. app.py만 교체하지 말고 \"MarketScout_v5_fixed\" 폴더의 "
        "app.py, database.py, engine.py, settings_store.py와 DB 파일을 모두 같은 저장소에 덮어쓰세요."
    )
    st.code(str(exc))
    st.stop()
from settings_store import delete_credentials, load_settings, save_settings

st.set_page_config(page_title="MarketScout v4", page_icon="📈", layout="wide")
st.title("📈 MarketScout")
st.caption("농수산물 품목 DB · 별칭 통합검색 · 검색오염 경고 · 최근 완료 3년 제철분석 v5")

settings = load_settings()
db_df = load_database_df()
products = category_map()

def secret_config():
    try:
        n=st.secrets["naver"]
        return ApiConfig(str(n["client_id"]),str(n["client_secret"]),str(n.get("auth_mode","hub")))
    except Exception: return None

def saved_config():
    if settings.get("client_id") and settings.get("client_secret"):
        return ApiConfig(settings["client_id"],settings["client_secret"],settings.get("auth_mode","hub"))
    return None

config=saved_config() or secret_config()
for k,v in {"results":None,"raw":None,"quick_result":None}.items(): st.session_state.setdefault(k,v)

def fmt_date(v):
    if not v or pd.isna(v): return "-"
    return pd.to_datetime(v).strftime("%m/%d")

def judgement_from_cached(row):
    if row["season_type_calculated"]=="사계절형": return "✅ 상시 진입 가능"
    today=date.today(); upload=pd.to_datetime(row["recommended_upload_date"]).date(); entry=pd.to_datetime(row["entry_date"]).date(); end=pd.to_datetime(row["expected_end_date"]).date()
    if today<upload:return f"⏳ {(upload-today).days}일 후 등록 추천"
    if today<entry:return "✅ 지금 등록·판매 준비"
    remain=(end-today).days
    if remain<0:return "❌ 시즌 종료"
    if remain>=30:return "✅ 지금 진입 가능"
    if remain>=15:return "⚠️ 짧게 판매 가능"
    return f"🔴 늦음 · {remain}일 남음"

tabs=st.tabs(["🏠 대시보드","🔍 품목 즉시판단","📆 월별 분석","📅 캘린더","🗂 DB 관리","⚙️ 설정"])

with tabs[1]:
    st.subheader("품목 즉시판단")
    q=st.text_input("업체에서 제안받은 품목명",placeholder="예: 마늘쫑, 마늘종, 황도")
    matches=search_items(q) if q.strip() else pd.DataFrame()
    selected=None
    if q.strip():
        if matches.empty:
            st.warning("현재 DB에 없는 품목입니다.")
            with st.expander("새 품목 추가",expanded=True):
                c1,c2,c3=st.columns(3)
                cat=c1.selectbox("카테고리",categories(),key="quick_new_cat")
                rep=c2.text_input("대표품목",value=q.strip(),key="quick_new_rep")
                stype=c3.selectbox("초기 판매유형",["미정","제철형","사계절형","복합형"],key="quick_new_type")
                if st.button("DB 추가 후 3년 분석",type="primary",disabled=config is None):
                    item_id=add_item(cat,rep,q.strip(),stype,"유통명" if rep!=q.strip() else "대표명")
                    found=search_items(q.strip()).iloc[0]
                    years=completed_years(); start=f"{years[0]}-01-01"; end=f"{years[-1]}-12-31"
                    with st.spinner(f"{', '.join(map(str,years))} 데이터 분석 중..."):
                        plan=keyword_plan(item_id,q.strip())
                        raw=collect_plans(config,[plan],start,end)
                        result=analyze_keyword(raw,plan["group_name"],date.today().year)
                        save_analysis(result,item_id,int(found["name_id"]))
                        st.session_state.quick_result=result
                    st.success("추가와 분석이 완료되었습니다."); st.rerun()
                if config is None: st.info("설정 탭에서 NAVER API 키를 먼저 저장하세요.")
        else:
            options=matches.index.tolist()
            idx=st.selectbox("DB 검색 결과",options,format_func=lambda i:f"{matches.loc[i,'검색명']} · {matches.loc[i,'대표품목']} · {matches.loc[i,'카테고리']} · {matches.loc[i,'이름유형']}")
            selected=matches.loc[idx]
            st.success(f"✅ DB 등록됨 · 대표품목 {selected['대표품목']} · {selected['카테고리']} · 초기분류 {selected['초기판매유형']}")
            details=get_item_details(int(selected["item_id"]))
            plan=keyword_plan(int(selected["item_id"]),str(selected["검색명"]))
            st.caption("데이터랩 통합 검색어: "+", ".join(plan["keywords"]))
            if plan["categories"]: st.caption("연결 카테고리: "+" · ".join(plan["categories"]))
            if plan["forms"]: st.caption("판매형태: "+" · ".join(plan["forms"]))
            if plan["exclude_keywords"]:
                st.warning("검색오염 주의어: "+", ".join(plan["exclude_keywords"])+" · 네이버 데이터랩은 제외 검색어를 직접 차감하지 않으므로 결과 해석 시 확인하세요.")
            cached=load_analysis(str(selected["검색명"]),date.today().year)
            c1,c2=st.columns(2)
            analyze_now=c1.button("최근 완료 3년 분석",type="primary",disabled=config is None,use_container_width=True)
            use_cached=c2.button("저장 결과 보기",disabled=cached.empty,use_container_width=True)
            if analyze_now:
                years=completed_years(); start=f"{years[0]}-01-01"; end=f"{years[-1]}-12-31"
                with st.spinner(f"{', '.join(map(str,years))} 데이터 수집·평균 계산 중..."):
                    plan=keyword_plan(int(selected["item_id"]),str(selected["검색명"]))
                    raw=collect_plans(config,[plan],start,end)
                    result=analyze_keyword(raw,plan["group_name"],date.today().year)
                    save_analysis(result,int(selected["item_id"]),int(selected["name_id"]))
                    st.session_state.quick_result=result; st.session_state.raw=raw
            elif use_cached:
                st.session_state.quick_result=cached.iloc[0].to_dict()
            result=st.session_state.quick_result
            if result and result.get("search_keyword")==str(selected["검색명"]):
                st.divider(); st.markdown(f"### {result['search_keyword']} 판단")
                a,b,c,d=st.columns(4)
                a.metric("판매유형",result["season_type_calculated"])
                b.metric("오늘 판단",result.get("judgement") or judgement_from_cached(pd.Series(result)))
                c.metric("계절성점수",f"{float(result.get('seasonality_score') or 0):.1f}")
                d.metric("신뢰도",f"{float(result.get('season_type_confidence') or 0):.0f}점")
                if result["season_type_calculated"]=="사계절형":
                    ch=result.get("recent_30d_change")
                    st.info(f"사계절 상품 · 최근 30일 변화율: {ch:+.1f}%" if ch is not None and not pd.isna(ch) else "사계절 상품 · 최근 추세 데이터 부족")
                else:
                    cols=st.columns(4)
                    cols[0].metric("추천 등록",fmt_date(result.get("recommended_upload_date")))
                    cols[1].metric("진입",fmt_date(result.get("entry_date")))
                    cols[2].metric("피크",fmt_date(result.get("expected_peak_date")))
                    cols[3].metric("종료",fmt_date(result.get("expected_end_date")))
                st.caption(f"분석연도: {result.get('analysis_years')} · 마지막 분석: {result.get('last_analyzed_at')}")
                raw=st.session_state.raw
                if raw is not None and str(selected['검색명']) in raw.columns: st.line_chart(raw[[str(selected['검색명'])]])

with tabs[2]:
    st.subheader("월별 제철·사계절 분석")
    a,b,c=st.columns(3)
    target_year=int(a.number_input("적용 연도",date.today().year,2035,date.today().year))
    target_month=int(b.selectbox("월",range(1,13),index=date.today().month-1,format_func=lambda x:f"{x}월"))
    selected_cats=c.multiselect("카테고리",list(products),default=[list(products)[0]] if products else [])
    st.caption("제철형은 해당 월과 판매기간이 겹치는 품목, 사계절형은 최근 추세와 함께 표시합니다.")
    if st.button("월별 분석 실행",type="primary",disabled=config is None or not selected_cats,use_container_width=True):
        items=[x for cat in selected_cats for x in products[cat]]
        plans=[]; seen_items=set()
        for name in items:
            m=search_items(name)
            if not m.empty:
                row=m.iloc[0]; iid=int(row["item_id"])
                if iid not in seen_items:
                    plans.append(keyword_plan(iid,str(row["검색명"]))); seen_items.add(iid)
        years=completed_years(); status=st.status("수집 중",expanded=True); bar=st.progress(0)
        def progress(n,total,batch): bar.progress(n/total); status.write(f"{n}/{total} · {', '.join(batch)}")
        try:
            raw=collect_plans(config,plans,f"{years[0]}-01-01",f"{years[-1]}-12-31",progress)
            canonical={cat:[] for cat in selected_cats}
            for plan in plans:
                primary=plan["categories"][0] if plan["categories"] else selected_cats[0]
                if primary in canonical: canonical[primary].append(plan["group_name"])
            results=analyze(raw,canonical,target_year,target_month)
            st.session_state.results=results; st.session_state.raw=raw
            for _,r in results.iterrows():
                m=search_items(str(r["품목"]))
                if not m.empty:
                    save_analysis({k:r.get(k) for k in ["search_keyword","target_year","analysis_years","season_type_calculated","season_type_confidence","recommended_upload_date","entry_date","expected_peak_date","expected_end_date","seasonality_score","offseason_retention_rate","peak_concentration_rate","year_consistency_rate","recent_30d_change","judgement","last_analyzed_at"]},int(m.iloc[0]["item_id"]),int(m.iloc[0]["name_id"]))
            status.update(label="완료",state="complete",expanded=False)
        except Exception as exc: status.update(label="실패",state="error"); st.exception(exc)
    results=st.session_state.results
    if results is not None:
        if results.empty: st.info("해당 월에 표시할 결과가 없습니다.")
        else:
            show=[x for x in ["카테고리순위","품목","판매유형","현재상태","등록시작일","진입일","피크일","종료일","추천행동","신뢰도","계절성점수"] if x in results.columns]
            st.dataframe(results[show],hide_index=True,use_container_width=True,height=620)
            st.download_button("Excel 다운로드",to_excel(results,st.session_state.raw),file_name=f"MarketScout_{target_year}_{target_month:02d}.xlsx")

with tabs[0]:
    st.subheader("오늘의 소싱 대시보드")
    cached=load_analysis(target_year=date.today().year)
    if cached.empty:
        st.info("저장된 분석 결과가 없습니다. 품목 즉시판단이나 월별 분석을 먼저 실행하세요.")
    else:
        cached["오늘판단"]=cached.apply(judgement_from_cached,axis=1)
        seasonal=cached[cached["season_type_calculated"]!="사계절형"].copy()
        evergreen=cached[cached["season_type_calculated"]=="사계절형"].copy()
        for col in ["recommended_upload_date","entry_date","expected_peak_date","expected_end_date"]:
            seasonal[col]=pd.to_datetime(seasonal[col],errors="coerce").dt.date
        today=date.today()
        m1,m2,m3,m4=st.columns(4)
        m1.metric("지금 등록·준비",int(seasonal["오늘판단"].str.contains("등록|준비",regex=True).sum()))
        m2.metric("지금 진입 가능",int(seasonal["오늘판단"].str.contains("진입 가능").sum()))
        m3.metric("곧 종료",int(seasonal["오늘판단"].str.contains("짧게|늦음",regex=True).sum()))
        m4.metric("사계절 분석품목",len(evergreen))
        st.markdown("### 📌 오늘 해야 할 일")
        tasks=[]
        for _,r in seasonal.iterrows():
            if r["recommended_upload_date"]==today: tasks.append({"할 일":"상품 등록","품목":r["search_keyword"],"판단":r["오늘판단"]})
            if r["entry_date"]==today: tasks.append({"할 일":"판매 시작","품목":r["search_keyword"],"판단":r["오늘판단"]})
            if r["expected_end_date"] and 0<=(r["expected_end_date"]-today).days<=7: tasks.append({"할 일":"재고 소진","품목":r["search_keyword"],"판단":r["오늘판단"]})
        st.dataframe(pd.DataFrame(tasks) if tasks else pd.DataFrame([{"할 일":"정기 확인","품목":"오늘 지정 일정 없음","판단":"월별 추천표 확인"}]),hide_index=True,use_container_width=True)
        st.markdown("### ⭐ 지금 우선 확인")
        priority=seasonal[seasonal["오늘판단"].str.contains("✅|⚠️",regex=True)].copy().sort_values(["seasonality_score","season_type_confidence"],ascending=False)
        st.dataframe(priority[["search_keyword","카테고리","오늘판단","recommended_upload_date","entry_date","expected_peak_date","expected_end_date","seasonality_score"]].head(20),hide_index=True,use_container_width=True)
        if not evergreen.empty:
            st.markdown("### 📈 사계절 최근 상승")
            st.dataframe(evergreen.sort_values("recent_30d_change",ascending=False)[["search_keyword","카테고리","recent_30d_change","season_type_confidence","last_analyzed_at"]].head(20),hide_index=True,use_container_width=True)

with tabs[3]:
    st.subheader("소싱 캘린더")
    cached=load_analysis(target_year=date.today().year)
    if cached.empty: st.info("저장된 분석 결과가 없습니다.")
    else:
        seasonal=cached[cached["season_type_calculated"]!="사계절형"]
        events=[]
        for _,r in seasonal.iterrows():
            for label,key in [("등록","recommended_upload_date"),("진입","entry_date"),("피크","expected_peak_date"),("종료","expected_end_date")]:
                if r[key]: events.append({"날짜":pd.to_datetime(r[key]).date(),"단계":label,"품목":r["search_keyword"],"카테고리":r["카테고리"]})
        ev=pd.DataFrame(events)
        if not ev.empty:
            month=st.selectbox("월 선택",range(1,13),index=date.today().month-1,format_func=lambda x:f"{x}월",key="calmonth")
            st.dataframe(ev[pd.to_datetime(ev["날짜"]).dt.month==month].sort_values(["날짜","단계","품목"]),hide_index=True,use_container_width=True,height=650)

with tabs[4]:
    st.subheader("품목 DB 관리")
    counts=db_df.groupby("대분류")["세부품목"].nunique().to_dict()
    st.success(f"검색 가능 품목명 {len(db_df):,}개 · 대표품목 {db_df['item_id'].nunique():,}개")
    st.caption(" · ".join(f"{k} {v:,}개" for k,v in counts.items()))
    issues=validate_database()
    if issues.empty: st.success("DB 구조검사 통과 · 대표명/주카테고리/별칭 충돌 이상 없음")
    else:
        st.warning(f"DB 검수 필요 {len(issues)}건")
        with st.expander("검수 항목 보기"): st.dataframe(issues,hide_index=True,use_container_width=True)
    search=st.text_input("DB 내 검색",key="db_search")
    view=db_df[db_df["세부품목"].str.contains(search,case=False,na=False)] if search else db_df
    st.dataframe(view[["대분류","기준품목","세부품목","이름유형","초기판매유형"]],hide_index=True,use_container_width=True,height=500)
    st.divider(); st.markdown("### 품목 추가")
    c1,c2,c3,c4=st.columns(4)
    cat=c1.selectbox("카테고리",categories(),key="db_cat")
    rep=c2.text_input("대표품목",key="db_rep")
    sub=c3.text_input("검색명/세부품목",key="db_sub")
    typ=c4.selectbox("초기 유형",["미정","제철형","사계절형","복합형"],key="db_type")
    if st.button("품목 DB에 추가"):
        add_item(cat,rep or sub,sub or rep,typ,"유통명" if rep and sub and rep!=sub else "대표명"); st.success("추가했습니다."); st.rerun()
    st.markdown("### 검색규칙·판매형태 추가")
    target=st.text_input("규칙을 넣을 대표품목/별칭",key="rule_target")
    found=search_items(target) if target.strip() else pd.DataFrame()
    if not found.empty:
        r=found.iloc[0]; iid=int(r["item_id"])
        x1,x2=st.columns(2)
        inc=x1.text_input("추가 통합검색어(쉼표)",key="rule_inc")
        exc=x2.text_input("검색오염 주의어(쉼표)",key="rule_exc")
        note=st.text_input("규칙 메모",key="rule_note")
        forms=st.multiselect("판매형태",product_forms(),key="rule_forms")
        if st.button("검색규칙/판매형태 저장"):
            if inc or exc or note: add_search_rule(iid,inc,exc,note)
            for form in forms: add_product_form(iid,form)
            st.success("저장했습니다."); st.rerun()

with tabs[5]:
    st.subheader("NAVER API 설정")
    modes={"hub":"NAVER API HUB","legacy_ncp":"NAVER Cloud 기존 방식","developer":"NAVER Developers 데이터랩"}
    mode=st.selectbox("인증 방식",list(modes),format_func=lambda x:modes[x],index=list(modes).index(settings.get("auth_mode","hub")) if settings.get("auth_mode","hub") in modes else 0)
    cid=st.text_input("Client ID",value=settings.get("client_id","")); secret=st.text_input("Client Secret",value=settings.get("client_secret",""),type="password")
    a,b,c=st.columns(3)
    if a.button("저장",type="primary",use_container_width=True):
        settings.update({"auth_mode":mode,"client_id":cid.strip(),"client_secret":secret.strip()}); save_settings(settings); st.success("저장했습니다."); st.rerun()
    if b.button("연결 테스트",use_container_width=True):
        try: call_api(ApiConfig(cid.strip(),secret.strip(),mode),["사과"],(date.today()-timedelta(days=30)).isoformat(),date.today().isoformat(),1); st.success("API 연결 성공")
        except Exception as exc: st.error(str(exc))
    if c.button("저장 키 삭제",use_container_width=True): delete_credentials(); st.success("삭제했습니다.")
