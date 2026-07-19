# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Dict, Iterable, List, Tuple
import time
import numpy as np
import pandas as pd
import requests

HUB_URL = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
LEGACY_NCP_URL = "https://naveropenapi.apigw.ntruss.com/datalab/v1/search"
DEVELOPER_URL = "https://openapi.naver.com/v1/datalab/search"
ANCHOR = "사과"

@dataclass(frozen=True)
class ApiConfig:
    client_id: str
    client_secret: str
    auth_mode: str = "hub"
    @property
    def url(self):
        return {"hub":HUB_URL,"legacy_ncp":LEGACY_NCP_URL,"developer":DEVELOPER_URL}.get(self.auth_mode,HUB_URL)
    @property
    def headers(self):
        if self.auth_mode == "developer":
            return {"X-Naver-Client-Id":self.client_id,"X-Naver-Client-Secret":self.client_secret,"Content-Type":"application/json"}
        return {"X-NCP-APIGW-API-KEY-ID":self.client_id,"X-NCP-APIGW-API-KEY":self.client_secret,"Content-Type":"application/json"}

class NaverApiError(RuntimeError): pass

def chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0,len(items),size): yield items[i:i+size]

def call_api(config: ApiConfig, keywords: List[str], start_date: str, end_date: str, retries: int=3) -> Dict[str,pd.Series]:
    body={"startDate":start_date,"endDate":end_date,"timeUnit":"date","keywordGroups":[{"groupName":k,"keywords":[k]} for k in keywords]}
    last=None
    for attempt in range(retries):
        try:
            r=requests.post(config.url,headers=config.headers,json=body,timeout=45)
            if r.status_code==200:
                out={}
                for item in r.json().get("results",[]):
                    s=pd.Series({x["period"]:float(x["ratio"]) for x in item.get("data",[])},dtype=float,name=item.get("title"))
                    s.index=pd.to_datetime(s.index); out[item.get("title")]=s.sort_index()
                return out
            if r.status_code in (429,500,502,503,504):
                last=f"HTTP {r.status_code}: {r.text[:500]}"; time.sleep(1.2*(attempt+1)); continue
            raise NaverApiError(f"NAVER API 오류 {r.status_code}: {r.text[:700]}")
        except requests.RequestException as exc:
            last=str(exc); time.sleep(1.2*(attempt+1))
    raise NaverApiError(f"NAVER API 호출 실패: {last}")

def collect(config: ApiConfig, targets: List[str], start_date: str, end_date: str, progress=None) -> pd.DataFrame:
    targets=[x for x in dict.fromkeys(targets) if x and x!=ANCHOR]
    merged={}; anchor_ref=None; batches=list(chunks(targets,4))
    for n,batch in enumerate(batches,1):
        if progress: progress(n,len(batches),batch)
        data=call_api(config,[ANCHOR]+batch,start_date,end_date)
        anchor=data.get(ANCHOR)
        if anchor is None or anchor.dropna().empty: raise NaverApiError("기준어 '사과' 데이터가 없습니다.")
        if anchor_ref is None:
            anchor_ref=anchor.copy(); merged[ANCHOR]=anchor_ref; scale=1.0
        else:
            common=anchor_ref.index.intersection(anchor.index)
            valid=(anchor_ref.reindex(common)>0)&(anchor.reindex(common)>0)
            ratios=anchor_ref.reindex(common)[valid]/anchor.reindex(common)[valid]
            scale=float(ratios.median()) if len(ratios) else 1.0
        for k in batch:
            if k in data: merged[k]=data[k]*scale
        time.sleep(.08)
    return pd.concat(merged,axis=1).sort_index() if merged else pd.DataFrame()

def _smooth(s): return s.fillna(0).rolling(7,center=True,min_periods=1).mean()
def _circular_mean_doy(doys):
    a=np.array(doys)/365.25*2*np.pi; angle=np.arctan2(np.sin(a).mean(),np.cos(a).mean())
    if angle<0: angle+=2*np.pi
    return max(1,min(366,int(round(angle/(2*np.pi)*365.25))))
def _date_from_doy(year,doy): return date(year,1,1)+timedelta(days=doy-1)
def _season_bounds(s,peak_idx):
    sm=_smooth(s); peak=float(sm.loc[peak_idx]); threshold=max(peak*.28,float(sm.quantile(.55)))
    active=sm>=threshold; pos=sm.index.get_loc(peak_idx); left=right=pos
    while left>0 and bool(active.iloc[left-1]): left-=1
    while right<len(sm)-1 and bool(active.iloc[right+1]): right+=1
    return sm.index[left],sm.index[right]

def completed_years(today: date|None=None, n:int=3)->List[int]:
    today=today or date.today(); return list(range(today.year-n,today.year))

def _judgement(season_type, upload, entry, end, today):
    if season_type=="사계절형": return "✅ 상시 진입 가능"
    if not entry or not end: return "⚠️ 판단 보류"
    if today<upload: return f"⏳ {(upload-today).days}일 후 등록 추천"
    if today<entry: return "✅ 지금 등록·판매 준비"
    remaining=(end-today).days
    if remaining<0: return "❌ 시즌 종료"
    if remaining>=30: return "✅ 지금 진입 가능"
    if remaining>=15: return "⚠️ 짧게 판매 가능"
    return f"🔴 늦음 · {remaining}일 남음"

def analyze_keyword(raw: pd.DataFrame, keyword: str, target_year: int|None=None, today: date|None=None) -> dict:
    today=today or date.today(); target_year=target_year or today.year
    years=completed_years(today,3)
    if keyword not in raw.columns: raise ValueError(f"'{keyword}' 데이터가 없습니다.")
    yearly=[]; monthly_tables=[]
    for y in years:
        sy=_smooth(raw.loc[raw.index.year==y,keyword])
        if sy.empty or sy.max()<=0: continue
        peak_idx=sy.idxmax(); start,end=_season_bounds(sy,peak_idx)
        yearly.append({"year":y,"peak":peak_idx.dayofyear,"start":start.dayofyear,"end":end.dayofyear,"max":float(sy.max())})
        monthly_tables.append(sy.groupby(sy.index.month).mean().reindex(range(1,13),fill_value=0))
    if len(yearly)<2: raise ValueError("유효한 연도 데이터가 2년 미만이라 분석할 수 없습니다.")
    monthly=pd.concat(monthly_tables,axis=1).mean(axis=1)
    mean=float(monthly.mean()); median=float(monthly.median()); peakm=float(monthly.max())
    cv=float(monthly.std()/(mean+1e-9)); active50=int((monthly>=peakm*.5).sum()) if peakm>0 else 12
    ratio=peakm/(median+1e-9)
    retention=float(monthly.min()/(peakm+1e-9)*100)
    concentration=float(monthly.nlargest(3).sum()/(monthly.sum()+1e-9)*100)
    if active50>=10 and ratio<1.6 and cv<.30:
        season_type="사계절형"
    elif active50>=7 and ratio<2.4:
        season_type="복합형"
    else:
        season_type="제철형"
    peak_spread=float(np.std([x["peak"] for x in yearly])); consistency=max(0,100-peak_spread*2.2)
    seasonality=min(100,max(0,(ratio-1)*50+cv*80+(12-active50)*5))
    conf=max(0,min(100, consistency*.7 + (100 if len(yearly)==3 else 65)*.3))
    upload=entry=peak=end=None
    if season_type!="사계절형":
        entry=_date_from_doy(target_year,_circular_mean_doy([x["start"] for x in yearly]))
        peak=_date_from_doy(target_year,_circular_mean_doy([x["peak"] for x in yearly]))
        end=_date_from_doy(target_year,_circular_mean_doy([x["end"] for x in yearly]))
        if entry>peak: entry=date(target_year-1,entry.month,entry.day)
        if end<peak: end=date(target_year+1,end.month,end.day)
        upload=entry-timedelta(days=14)
    recent=raw[keyword].dropna()
    recent_change=None
    if len(recent)>=60:
        a=float(recent.iloc[-30:].mean()); b=float(recent.iloc[-60:-30].mean())
        recent_change=(a-b)/(b+1e-9)*100
    result={
        "search_keyword":keyword,"target_year":target_year,"analysis_years":", ".join(map(str,[x['year'] for x in yearly])),
        "season_type_calculated":season_type,"season_type_confidence":round(conf,1),
        "recommended_upload_date":upload.isoformat() if upload else None,
        "entry_date":entry.isoformat() if entry else None,"expected_peak_date":peak.isoformat() if peak else None,
        "expected_end_date":end.isoformat() if end else None,"seasonality_score":round(seasonality,1),
        "offseason_retention_rate":round(retention,1),"peak_concentration_rate":round(concentration,1),
        "year_consistency_rate":round(consistency,1),"recent_30d_change":round(recent_change,1) if recent_change is not None else None,
        "judgement":_judgement(season_type,upload,entry,end,today),"last_analyzed_at":datetime.now().isoformat(timespec="seconds")
    }
    return result

def analyze(raw: pd.DataFrame, category_map: Dict[str,List[str]], target_year:int, target_month:int, today:date|None=None)->pd.DataFrame:
    today=today or date.today(); reverse={p:c for c,items in category_map.items() for p in items}; rows=[]
    for keyword in raw.columns:
        if keyword==ANCHOR or keyword not in reverse: continue
        try: r=analyze_keyword(raw,keyword,target_year,today)
        except Exception: continue
        if r["season_type_calculated"]=="사계절형":
            rows.append({"카테고리":reverse[keyword],"품목":keyword,"판매유형":"사계절형","현재상태":"상시판매","추천행동":"최근 추세 확인","신뢰도":"상" if r['season_type_confidence']>=80 else '중',"계절성점수":r['seasonality_score'],**r})
            continue
        entry=pd.to_datetime(r["entry_date"]).date(); end=pd.to_datetime(r["expected_end_date"]).date(); peak=pd.to_datetime(r["expected_peak_date"]).date(); upload=pd.to_datetime(r["recommended_upload_date"]).date()
        month_start=date(target_year,target_month,1); month_end=(date(target_year+1,1,1)-timedelta(days=1)) if target_month==12 else date(target_year,target_month+1,1)-timedelta(days=1)
        if max(0,(min(end,month_end)-max(entry,month_start)).days+1)<=0: continue
        rows.append({"카테고리":reverse[keyword],"품목":keyword,"판매유형":r['season_type_calculated'],"등록시작일":upload,"재고확보일":entry-timedelta(days=7),"광고준비일":entry-timedelta(days=3),"진입일":entry,"피크일":peak,"종료임박일":end-timedelta(days=7),"종료일":end,"판매기간(일)":(end-entry).days+1,"현재상태":r['judgement'],"추천행동":r['judgement'],"신뢰도":"상" if r['season_type_confidence']>=80 else ('중' if r['season_type_confidence']>=60 else '하'),"계절성점수":r['seasonality_score'],**r})
    out=pd.DataFrame(rows)
    if not out.empty:
        out=out.sort_values(["카테고리","계절성점수"],ascending=[True,False]).reset_index(drop=True); out["카테고리순위"]=out.groupby("카테고리").cumcount()+1
    return out

def to_excel(results:pd.DataFrame,raw:pd.DataFrame)->bytes:
    bio=BytesIO()
    with pd.ExcelWriter(bio,engine="openpyxl") as w:
        results.to_excel(w,index=False,sheet_name="분석결과"); raw.to_excel(w,sheet_name="일별원자료")
    return bio.getvalue()


def call_api_groups(config: ApiConfig, groups: List[dict], start_date: str, end_date: str, retries: int = 3) -> Dict[str, pd.Series]:
    """groups=[{'group_name': '황도', 'keywords': ['황도','황도복숭아']}, ...]."""
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "timeUnit": "date",
        "keywordGroups": [
            {"groupName": g["group_name"], "keywords": list(dict.fromkeys(g.get("keywords") or [g["group_name"]]))[:20]}
            for g in groups
        ],
    }
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(config.url, headers=config.headers, json=body, timeout=45)
            if r.status_code == 200:
                out = {}
                for item in r.json().get("results", []):
                    s = pd.Series({x["period"]: float(x["ratio"]) for x in item.get("data", [])}, dtype=float, name=item.get("title"))
                    s.index = pd.to_datetime(s.index)
                    out[item.get("title")] = s.sort_index()
                return out
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}: {r.text[:500]}"
                time.sleep(1.2 * (attempt + 1))
                continue
            raise NaverApiError(f"NAVER API 오류 {r.status_code}: {r.text[:700]}")
        except requests.RequestException as exc:
            last = str(exc)
            time.sleep(1.2 * (attempt + 1))
    raise NaverApiError(f"NAVER API 호출 실패: {last}")


def collect_plans(config: ApiConfig, plans: List[dict], start_date: str, end_date: str, progress=None) -> pd.DataFrame:
    """대표품목/품종/별칭을 한 검색그룹으로 묶어 분석한다. 제외어는 DB 오염 경고용이며 API에서 직접 차감하지 않는다."""
    unique = []
    seen = set()
    for p in plans:
        name = p["group_name"]
        if name and name != ANCHOR and name not in seen:
            unique.append(p)
            seen.add(name)
    merged = {}
    anchor_ref = None
    batches = list(chunks(unique, 4))
    for n, batch in enumerate(batches, 1):
        if progress:
            progress(n, len(batches), [x["group_name"] for x in batch])
        groups = [{"group_name": ANCHOR, "keywords": [ANCHOR]}] + [
            {"group_name": x["group_name"], "keywords": x.get("keywords") or [x["group_name"]]} for x in batch
        ]
        data = call_api_groups(config, groups, start_date, end_date)
        anchor = data.get(ANCHOR)
        if anchor is None or anchor.dropna().empty:
            raise NaverApiError("기준어 '사과' 데이터가 없습니다.")
        if anchor_ref is None:
            anchor_ref = anchor.copy()
            merged[ANCHOR] = anchor_ref
            scale = 1.0
        else:
            common = anchor_ref.index.intersection(anchor.index)
            valid = (anchor_ref.reindex(common) > 0) & (anchor.reindex(common) > 0)
            ratios = anchor_ref.reindex(common)[valid] / anchor.reindex(common)[valid]
            scale = float(ratios.median()) if len(ratios) else 1.0
        for p in batch:
            k = p["group_name"]
            if k in data:
                merged[k] = data[k] * scale
        time.sleep(.08)
    return pd.concat(merged, axis=1).sort_index() if merged else pd.DataFrame()
