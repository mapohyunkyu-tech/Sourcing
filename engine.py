# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Dict, Iterable, List, Tuple

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
    auth_mode: str = "developer"

    @property
    def url(self) -> str:
        return {"hub": HUB_URL, "legacy_ncp": LEGACY_NCP_URL, "developer": DEVELOPER_URL}.get(self.auth_mode, HUB_URL)

    @property
    def headers(self) -> Dict[str, str]:
        if self.auth_mode == "developer":
            return {"X-Naver-Client-Id": self.client_id, "X-Naver-Client-Secret": self.client_secret, "Content-Type": "application/json"}
        return {"X-NCP-APIGW-API-KEY-ID": self.client_id, "X-NCP-APIGW-API-KEY": self.client_secret, "Content-Type": "application/json"}

class NaverApiError(RuntimeError):
    pass

def chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i:i+size]

def call_api(config: ApiConfig, keywords: List[str], start_date: str, end_date: str, retries: int = 3) -> Dict[str, pd.Series]:
    body = {"startDate": start_date, "endDate": end_date, "timeUnit": "date", "keywordGroups": [{"groupName": k, "keywords": [k]} for k in keywords]}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(config.url, headers=config.headers, json=body, timeout=45)
            if r.status_code == 200:
                out = {}
                for item in r.json().get("results", []):
                    s = pd.Series({row["period"]: float(row["ratio"]) for row in item.get("data", [])}, dtype=float, name=item.get("title"))
                    s.index = pd.to_datetime(s.index)
                    out[item.get("title")] = s.sort_index()
                return out
            detail = r.text[:700]
            if r.status_code == 401:
                if "024" in detail:
                    raise NaverApiError("인증 범위 오류(024)입니다. 선택한 인증 방식과 키 발급처가 일치하는지, 해당 애플리케이션에 Search Trend 권한이 연결됐는지 확인하세요.")
                if '"errorCode":"200"' in detail.replace(" ", "") or "Authentication Failed" in detail:
                    mode_name = {"hub": "NAVER API HUB", "legacy_ncp": "기존 NAVER Cloud Search Trend", "developer": "NAVER Developers"}.get(config.auth_mode, config.auth_mode)
                    raise NaverApiError(
                        f"인증 실패(401/200)입니다. 현재 선택: {mode_name}. "
                        "Client ID·Secret 오타, 키 발급처와 인증 방식 불일치, 폐기·재발급된 키를 확인하세요. "
                        "Streamlit Cloud Secrets에 예전 키가 있으면 설정 탭에 저장한 키보다 우선 적용될 수 있으므로 Secrets도 확인하세요."
                    )
            if r.status_code == 429:
                raise NaverApiError(f"HTTP 429: {detail}")
            if r.status_code in (500, 502, 503, 504):
                last = f"HTTP {r.status_code}: {detail}"
                time.sleep(1.2 * (attempt + 1)); continue
            raise NaverApiError(f"NAVER API 오류 {r.status_code}: {detail}")
        except requests.RequestException as exc:
            last = str(exc); time.sleep(1.2 * (attempt + 1))
    raise NaverApiError(f"NAVER API 호출 실패: {last}")

def collect(config: ApiConfig, targets: List[str], start_date: str, end_date: str, progress=None) -> pd.DataFrame:
    targets = [x for x in dict.fromkeys(targets) if x != ANCHOR]
    merged: Dict[str, pd.Series] = {}
    anchor_ref = None
    batches = list(chunks(targets, 4))
    for n, batch in enumerate(batches, 1):
        if progress: progress(n, len(batches), batch)
        data = call_api(config, [ANCHOR] + batch, start_date, end_date)
        anchor = data.get(ANCHOR)
        if anchor is None or anchor.dropna().empty:
            raise NaverApiError("기준어 '사과' 데이터가 없어 품목 간 지수를 보정할 수 없습니다.")
        if anchor_ref is None:
            anchor_ref = anchor.copy(); merged[ANCHOR] = anchor_ref; scale = 1.0
        else:
            common = anchor_ref.index.intersection(anchor.index)
            valid = (anchor_ref.reindex(common) > 0) & (anchor.reindex(common) > 0)
            ratios = anchor_ref.reindex(common)[valid] / anchor.reindex(common)[valid]
            scale = float(ratios.median()) if len(ratios) else 1.0
        for k in batch:
            if k in data: merged[k] = data[k] * scale
        time.sleep(0.08)
    if not merged: return pd.DataFrame()
    return pd.concat(merged, axis=1).sort_index()

def _smooth(s: pd.Series) -> pd.Series:
    return s.fillna(0).rolling(7, center=True, min_periods=1).mean()

def _circular_mean_doy(doys: List[int]) -> int:
    angles = np.array(doys) / 365.25 * 2 * np.pi
    angle = np.arctan2(np.sin(angles).mean(), np.cos(angles).mean())
    if angle < 0: angle += 2*np.pi
    return max(1, min(366, int(round(angle / (2*np.pi) * 365.25))))

def _date_from_doy(year: int, doy: int) -> date:
    return date(year,1,1) + timedelta(days=doy-1)

def _season_bounds(s: pd.Series, peak_idx: pd.Timestamp) -> Tuple[pd.Timestamp,pd.Timestamp]:
    sm = _smooth(s)
    peak = float(sm.loc[peak_idx])
    threshold = max(peak * 0.28, float(sm.quantile(.55)))
    active = sm >= threshold
    pos = sm.index.get_loc(peak_idx)
    left = pos
    while left > 0 and bool(active.iloc[left-1]): left -= 1
    right = pos
    while right < len(sm)-1 and bool(active.iloc[right+1]): right += 1
    return sm.index[left], sm.index[right]

def _status(entry: date, peak: date, end: date, today: date) -> Tuple[str,str,int]:
    register = entry - timedelta(days=14)
    stock = entry - timedelta(days=7)
    ad = entry - timedelta(days=3)
    if today < register:
        d=(register-today).days
        return ("등록 준비 전", f"{d}일 후 상품 등록", (entry-today).days)
    if today < stock:
        return ("상품 등록 기간", "상세페이지 제작·상품 등록", (entry-today).days)
    if today < ad:
        return ("재고 확보 기간", "공급처 확인·발주", (entry-today).days)
    if today < entry:
        return ("판매 직전", "가격 점검·광고 준비", (entry-today).days)
    if today <= peak: return ("진입 가능 · 피크 전", "판매 시작·광고 확대", 0)
    if today <= end - timedelta(days=7): return ("판매 가능 · 피크 후", "재고를 보수적으로 운영", 0)
    if today <= end: return ("종료 임박", "광고 축소·재고 소진", 0)
    return ("시즌 종료", "다음 시즌 준비", 0)

def analyze(raw: pd.DataFrame, category_map: Dict[str,List[str]], target_year: int, target_month: int, today: date | None=None) -> pd.DataFrame:
    today = today or date.today()
    rows=[]
    years=sorted(set(raw.index.year))
    if target_year in years and date(target_year,target_month,1) > today:
        years=[y for y in years if y < target_year]
    years=years[-3:]
    reverse={p:c for c,items in category_map.items() for p in items}
    for product in raw.columns:
        if product == ANCHOR or product not in reverse: continue
        yearly=[]
        monthly_means=[]
        all_month_means=[]
        zero_ratios=[]
        for y in years:
            sy=_smooth(raw.loc[raw.index.year==y, product])
            if sy.empty or sy.max() <= 0: continue
            peak_idx=sy.idxmax(); start,end=_season_bounds(sy,peak_idx)
            yearly.append((y,peak_idx.dayofyear,start.dayofyear,end.dayofyear,float(sy.max())))
            monthly=sy.groupby(sy.index.month).mean().reindex(range(1,13),fill_value=0)
            all_month_means.append(monthly)
            monthly_means.append(float(monthly.loc[target_month]))
            zero_ratios.append(float((sy<=0).mean()))
        if len(yearly)<2: continue
        monthly_avg=pd.concat(all_month_means,axis=1).mean(axis=1)
        median=float(monthly_avg.median()); peakm=float(monthly_avg.max())
        cv=float(monthly_avg.std()/(monthly_avg.mean()+1e-9))
        active50=int((monthly_avg >= peakm*.5).sum()) if peakm>0 else 12
        peak_median_ratio=peakm/(median+1e-9)
        evergreen=(active50>=10 and peak_median_ratio<1.6 and cv<.30)
        if evergreen: continue
        peak_doy=_circular_mean_doy([x[1] for x in yearly])
        entry_doy=_circular_mean_doy([x[2] for x in yearly])
        end_doy=_circular_mean_doy([x[3] for x in yearly])
        entry=_date_from_doy(target_year,entry_doy); peak=_date_from_doy(target_year,peak_doy); end=_date_from_doy(target_year,end_doy)
        if entry > peak: entry=date(target_year-1,entry.month,entry.day)
        if end < peak: end=date(target_year+1,end.month,end.day)
        month_start=date(target_year,target_month,1)
        month_end=(date(target_year+1,1,1)-timedelta(days=1)) if target_month==12 else (date(target_year,target_month+1,1)-timedelta(days=1))
        overlap=max(0,(min(end,month_end)-max(entry,month_start)).days+1)
        if overlap<=0: continue
        peak_spread=float(np.std([x[1] for x in yearly]))
        consistency=max(0,100-peak_spread*2.2)
        month_strength=float(np.mean(monthly_means))/(peakm+1e-9)*100
        seasonality=min(100,max(0,(peak_median_ratio-1)*50 + cv*80 + (12-active50)*5))
        score=month_strength*.45+consistency*.30+seasonality*.25
        confidence="상" if len(yearly)==3 and peak_spread<=18 else ("중" if peak_spread<=35 else "하")
        state,action,days=_status(entry,peak,end,today)
        rows.append({"카테고리":reverse[product],"품목":product,"등록시작일":entry-timedelta(days=14),"재고확보일":entry-timedelta(days=7),"광고준비일":entry-timedelta(days=3),"진입일":entry,"피크일":peak,"종료임박일":end-timedelta(days=7),"종료일":end,"판매기간(일)":(end-entry).days+1,"현재상태":state,"진입까지(일)":days,"추천행동":action,"신뢰도":confidence,"계절성점수":round(score,1),"피크편차(일)":round(peak_spread,1),"분석연도":", ".join(map(str,years))})
    out=pd.DataFrame(rows)
    if out.empty:return out
    out=out.sort_values(["카테고리","계절성점수","신뢰도"],ascending=[True,False,True]).reset_index(drop=True)
    out["카테고리순위"]=out.groupby("카테고리").cumcount()+1
    return out

def to_excel(results: pd.DataFrame, raw: pd.DataFrame) -> bytes:
    bio=BytesIO()
    with pd.ExcelWriter(bio,engine="openpyxl") as w:
        results.to_excel(w,index=False,sheet_name="카테고리_TOP")
        raw.to_excel(w,sheet_name="일별원자료")
    return bio.getvalue()

# ------------------------------------------------------------------
# Streamlit app compatibility / detailed single-keyword season model
# ------------------------------------------------------------------
def completed_years(today: date | None = None, count: int = 3) -> List[int]:
    """Return the latest fully completed calendar years."""
    today = today or date.today()
    last = today.year - 1
    return list(range(last - count + 1, last + 1))


def _iso(v: date | datetime | pd.Timestamp | None) -> str | None:
    if v is None:
        return None
    return pd.Timestamp(v).date().isoformat()


def _safe_date(year: int, month: int, day: int) -> date:
    # Handles leap-day averages safely.
    while day > 28:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
    return date(year, month, day)


def _project_doy(target_year: int, doy: int) -> date:
    max_doy = 366 if pd.Timestamp(target_year, 12, 31).dayofyear == 366 else 365
    return date(target_year, 1, 1) + timedelta(days=max(1, min(max_doy, int(doy))) - 1)


def _season_profile_for_year(series: pd.Series, year: int) -> dict | None:
    sy = series.loc[series.index.year == year].astype(float).sort_index()
    if sy.empty:
        return None
    full_idx = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    sy = sy.reindex(full_idx).fillna(0.0)
    sm = _smooth(sy)
    peak_value = float(sm.max())
    if peak_value <= 0:
        return None
    peak_ts = pd.Timestamp(sm.idxmax())
    baseline = float(sm.quantile(0.50))
    start_threshold = max(peak_value * 0.20, baseline * 1.15)
    end_threshold = max(peak_value * 0.18, baseline * 1.05)
    peak_threshold = peak_value * 0.85

    peak_pos = int(sm.index.get_loc(peak_ts))
    left = peak_pos
    while left > 0 and float(sm.iloc[left - 1]) >= start_threshold:
        left -= 1
    right = peak_pos
    while right < len(sm) - 1 and float(sm.iloc[right + 1]) >= end_threshold:
        right += 1

    pleft = peak_pos
    while pleft > 0 and float(sm.iloc[pleft - 1]) >= peak_threshold:
        pleft -= 1
    pright = peak_pos
    while pright < len(sm) - 1 and float(sm.iloc[pright + 1]) >= peak_threshold:
        pright += 1

    return {
        "year": year,
        "peak_doy": peak_ts.dayofyear,
        "peak_date": peak_ts.date(),
        "start_doy": sm.index[left].dayofyear,
        "end_doy": sm.index[right].dayofyear,
        "peak_start_doy": sm.index[pleft].dayofyear,
        "peak_end_doy": sm.index[pright].dayofyear,
        "peak_value": peak_value,
        "monthly": sm.groupby(sm.index.month).mean().reindex(range(1, 13), fill_value=0.0),
    }


def analyze_keyword(raw: pd.DataFrame, keyword: str, target_year: int | None = None, today: date | None = None) -> dict:
    """Analyze one keyword and return the payload expected by app.py/database.py."""
    today = today or date.today()
    target_year = int(target_year or today.year)
    if keyword not in raw.columns:
        raise ValueError(f"원자료에 '{keyword}' 열이 없습니다.")

    s = raw[keyword].copy()
    s.index = pd.to_datetime(s.index)
    candidate_years = sorted(set(int(y) for y in s.index.year))
    profiles = [p for y in candidate_years if (p := _season_profile_for_year(s, y)) is not None]
    profiles = profiles[-3:]
    if len(profiles) < 2:
        raise ValueError("시즌 계산에 필요한 유효 연도 데이터가 2개 미만입니다.")

    monthly_df = pd.concat([p["monthly"] for p in profiles], axis=1)
    monthly_avg = monthly_df.mean(axis=1)
    peak_month_value = float(monthly_avg.max())
    avg_month_value = float(monthly_avg.mean())
    active_months = int((monthly_avg >= peak_month_value * 0.50).sum()) if peak_month_value > 0 else 12
    cv = float(monthly_avg.std() / (avg_month_value + 1e-9))
    peak_to_median = peak_month_value / (float(monthly_avg.median()) + 1e-9)
    evergreen = active_months >= 10 and peak_to_median < 1.65 and cv < 0.35

    peak_spread = float(np.std([p["peak_doy"] for p in profiles]))
    consistency = max(0.0, min(100.0, 100.0 - peak_spread * 2.0))
    seasonality = max(0.0, min(100.0, (peak_to_median - 1.0) * 40.0 + cv * 70.0 + (12 - active_months) * 5.0))
    confidence = round(consistency * 0.55 + seasonality * 0.45, 1)

    recent = _smooth(s).iloc[-30:]
    if len(recent) >= 2 and float(recent.iloc[0]) > 0:
        recent_change = round((float(recent.iloc[-1]) / float(recent.iloc[0]) - 1.0) * 100.0, 1)
    else:
        recent_change = 0.0

    base = {
        "search_keyword": keyword,
        "target_year": target_year,
        "analysis_years": ", ".join(str(p["year"]) for p in profiles),
        "yearly_peak_dates": ", ".join(f"{p['year']} {p['peak_date'].strftime('%m/%d')}" for p in profiles),
        "season_type_confidence": confidence,
        "recent_30d_change": recent_change,
        "last_analyzed_at": datetime.now().isoformat(timespec="seconds"),
    }

    if evergreen:
        return {
            **base,
            "season_type_calculated": "사계절형",
            "judgement": "상시 판매 가능",
            "recommended_upload_date": None,
            "entry_date": None,
            "season_start_date": None,
            "expected_peak_date": None,
            "expected_peak_start_date": None,
            "expected_peak_end_date": None,
            "gentle_decline_start_date": None,
            "expected_end_date": None,
            "remaining_sales_days": 365,
            "season_progress": 0,
            "recommended_action": "상시 판매 · 최근 추세에 맞춰 재고 조절",
        }

    entry_doy = _circular_mean_doy([p["start_doy"] for p in profiles])
    peak_doy = _circular_mean_doy([p["peak_doy"] for p in profiles])
    peak_start_doy = _circular_mean_doy([p["peak_start_doy"] for p in profiles])
    peak_end_doy = _circular_mean_doy([p["peak_end_doy"] for p in profiles])
    end_doy = _circular_mean_doy([p["end_doy"] for p in profiles])

    entry = _project_doy(target_year, entry_doy)
    peak = _project_doy(target_year, peak_doy)
    peak_start = _project_doy(target_year, peak_start_doy)
    peak_end = _project_doy(target_year, peak_end_doy)
    end = _project_doy(target_year, end_doy)

    # Handle seasons crossing New Year.
    if entry > peak:
        entry = _project_doy(target_year - 1, entry_doy)
    if peak_start < entry:
        peak_start = _project_doy(target_year, peak_start_doy)
    if peak_end < peak_start:
        peak_end = _project_doy(target_year + 1, peak_end_doy)
    if end < peak:
        end = _project_doy(target_year + 1, end_doy)

    upload = entry - timedelta(days=14)
    season_start = entry
    decline = peak_end + timedelta(days=1)
    total_days = max(1, (end - entry).days + 1)
    elapsed = min(max((today - entry).days, 0), total_days)
    progress = round(elapsed / total_days * 100.0, 1)
    remaining = max(0, (end - today).days + 1) if today >= entry else max(0, (end - entry).days + 1)
    judgement, action, _ = _status(entry, peak, end, today)

    return {
        **base,
        "season_type_calculated": "제철형",
        "judgement": judgement,
        "recommended_upload_date": _iso(upload),
        "entry_date": _iso(entry),
        "season_start_date": _iso(season_start),
        "expected_peak_date": _iso(peak),
        "expected_peak_start_date": _iso(peak_start),
        "expected_peak_end_date": _iso(peak_end),
        "gentle_decline_start_date": _iso(decline),
        "expected_end_date": _iso(end),
        "remaining_sales_days": int(remaining),
        "season_progress": progress,
        "recommended_action": action,
    }


# Override the earlier monthly analyzer with the payload shape used by app.py.
def analyze(raw: pd.DataFrame, category_map: Dict[str, List[str]], target_year: int, target_month: int, today: date | None = None) -> pd.DataFrame:
    today = today or date.today()
    rows: List[dict] = []
    for category, items in category_map.items():
        for keyword in items:
            if keyword == ANCHOR or keyword not in raw.columns:
                continue
            try:
                r = analyze_keyword(raw, keyword, target_year=target_year, today=today)
            except ValueError:
                continue
            if r["season_type_calculated"] == "사계절형":
                # All-year items are relevant in every month.
                include = True
            else:
                start = pd.to_datetime(r["entry_date"]).date()
                end = pd.to_datetime(r["expected_end_date"]).date()
                month_start = date(target_year, target_month, 1)
                month_end = (date(target_year + 1, 1, 1) - timedelta(days=1)) if target_month == 12 else (date(target_year, target_month + 1, 1) - timedelta(days=1))
                include = max(start, month_start) <= min(end, month_end)
            if include:
                rows.append({"카테고리": category, "품목": keyword, **r})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(["season_type_confidence", "카테고리", "품목"], ascending=[False, True, True]).reset_index(drop=True)
