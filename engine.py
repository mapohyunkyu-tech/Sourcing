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
        return {"hub": HUB_URL, "legacy_ncp": LEGACY_NCP_URL, "developer": DEVELOPER_URL}.get(self.auth_mode, DEVELOPER_URL)

    @property
    def headers(self) -> Dict[str, str]:
        if self.auth_mode == "developer":
            return {"X-Naver-Client-Id": self.client_id, "X-Naver-Client-Secret": self.client_secret, "Content-Type": "application/json"}
        return {"X-NCP-APIGW-API-KEY-ID": self.client_id, "X-NCP-APIGW-API-KEY": self.client_secret, "Content-Type": "application/json"}

class NaverApiError(RuntimeError):
    pass

def completed_years(today: date | None = None, count: int = 3) -> List[int]:
    today = today or date.today()
    return list(range(today.year - count, today.year))

def chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]

def call_api(config: ApiConfig, keywords: List[str], start_date: str, end_date: str, retries: int = 3) -> Dict[str, pd.Series]:
    body = {
        "startDate": start_date, "endDate": end_date, "timeUnit": "date",
        "keywordGroups": [{"groupName": k, "keywords": [k]} for k in keywords],
    }
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(config.url, headers=config.headers, json=body, timeout=45)
            if r.status_code == 200:
                out = {}
                for item in r.json().get("results", []):
                    s = pd.Series({row["period"]: float(row["ratio"]) for row in item.get("data", [])}, dtype=float)
                    s.index = pd.to_datetime(s.index)
                    out[item.get("title")] = s.sort_index()
                return out
            detail = r.text[:700]
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}: {detail}"; time.sleep(1.2 * (attempt + 1)); continue
            raise NaverApiError(f"NAVER API 오류 {r.status_code}: {detail}")
        except requests.RequestException as exc:
            last = str(exc); time.sleep(1.2 * (attempt + 1))
    raise NaverApiError(f"NAVER API 호출 실패: {last}")

def collect(config: ApiConfig, targets: List[str], start_date: str, end_date: str, progress=None) -> pd.DataFrame:
    targets = [x for x in dict.fromkeys(str(t).strip() for t in targets) if x and x != ANCHOR]
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
    return pd.concat(merged, axis=1).sort_index() if merged else pd.DataFrame()

def _smooth(s: pd.Series) -> pd.Series:
    return s.fillna(0).rolling(7, center=True, min_periods=1).mean()

def _circular_mean_doy(doys: List[int]) -> int:
    angles = np.array(doys) / 365.25 * 2 * np.pi
    angle = np.arctan2(np.sin(angles).mean(), np.cos(angles).mean())
    if angle < 0: angle += 2 * np.pi
    return max(1, min(366, int(round(angle / (2 * np.pi) * 365.25))))

def _date_from_doy(year: int, doy: int) -> date:
    return date(year, 1, 1) + timedelta(days=doy - 1)

def _contiguous_bounds(mask: pd.Series, center_pos: int) -> Tuple[int, int]:
    left = right = center_pos
    while left > 0 and bool(mask.iloc[left - 1]): left -= 1
    while right < len(mask) - 1 and bool(mask.iloc[right + 1]): right += 1
    return left, right

def _year_features(s: pd.Series) -> dict | None:
    sm = _smooth(s)
    if sm.empty or float(sm.max()) <= 0: return None
    peak_idx = sm.idxmax(); peak = float(sm.loc[peak_idx]); pos = sm.index.get_loc(peak_idx)
    peak_mask = sm >= peak * 0.90
    pl, pr = _contiguous_bounds(peak_mask, pos)
    season_threshold = max(peak * 0.28, float(sm.quantile(.55)))
    sl, sr = _contiguous_bounds(sm >= season_threshold, pos)
    # 진입일: 피크의 40%를 넘고 7일 추세가 상승하는 첫 날짜
    grad = sm.diff(7)
    candidates = sm[(sm >= peak * .40) & (grad > 0) & (sm.index <= peak_idx)]
    entry_idx = candidates.index[0] if len(candidates) else sm.index[sl]
    # 후반 판매: 피크 이후 35% 이상 유지되는 마지막 날
    post = sm.loc[peak_idx:]
    end_candidates = post[post >= peak * .35]
    end_idx = end_candidates.index[-1] if len(end_candidates) else sm.index[sr]
    return {
        "peak": peak_idx, "peak_start": sm.index[pl], "peak_end": sm.index[pr],
        "season_start": sm.index[sl], "entry": entry_idx, "end": end_idx,
        "peak_value": peak,
    }

def _date_status(entry: date, peak_start: date, peak: date, peak_end: date, end: date, today: date) -> Tuple[str, str]:
    prepare = entry - timedelta(days=14)
    if today < prepare: return "준비 전", "아직 등록하지 말고 공급처만 확인"
    if today < entry: return "준비", "상세페이지 제작·상품 등록 준비"
    if today < peak_start: return "상승 초입", "판매 시작·입고 확대"
    if today <= peak_end: return "피크 구간", "광고·판매 집중"
    if today <= end - timedelta(days=7): return "완만한 하락 판매 가능", "계속 판매 가능·추가입고는 보수적"
    if today <= end: return "종료 임박", "추가입고 중단·재고 소진"
    return "종료", "다음 시즌 준비"

def analyze_keyword(raw: pd.DataFrame, keyword: str, target_year: int, today: date | None = None) -> dict:
    today = today or date.today()
    if keyword not in raw.columns:
        raise ValueError(f"'{keyword}' 데이터가 없습니다.")
    years = sorted(set(raw.index.year))[-3:]
    feats = []
    monthly = []
    for y in years:
        sy = raw.loc[raw.index.year == y, keyword]
        f = _year_features(sy)
        if f:
            feats.append((y, f))
            monthly.append(_smooth(sy).groupby(sy.index.month).mean().reindex(range(1, 13), fill_value=0))
    if len(feats) < 2:
        raise ValueError("최소 2개 연도의 유효 데이터가 필요합니다.")
    monthly_avg = pd.concat(monthly, axis=1).mean(axis=1)
    peakm = float(monthly_avg.max()); median = float(monthly_avg.median())
    cv = float(monthly_avg.std() / (monthly_avg.mean() + 1e-9))
    active50 = int((monthly_avg >= peakm * .5).sum()) if peakm > 0 else 12
    evergreen = active50 >= 10 and peakm / (median + 1e-9) < 1.6 and cv < .30
    peak_doys = [f["peak"].dayofyear for _, f in feats]
    spread = float(np.std(peak_doys))
    confidence = max(0.0, min(100.0, 100 - spread * 2.0 - (3 - len(feats)) * 15))
    now = datetime.now().isoformat(timespec="seconds")
    if evergreen:
        s = _smooth(raw[keyword])
        recent = float((s.iloc[-1] / (s.iloc[-31] + 1e-9) - 1) * 100) if len(s) >= 31 else np.nan
        return {
            "search_keyword": keyword, "target_year": target_year, "analysis_years": ", ".join(map(str, years)),
            "season_type_calculated": "사계절형", "season_type_confidence": round(confidence, 1),
            "recent_30d_change": round(recent, 1) if not np.isnan(recent) else None,
            "seasonality_score": round(cv * 100, 1), "judgement": "상시 판매 가능",
            "last_analyzed_at": now,
        }
    def avg_date(key: str) -> date:
        return _date_from_doy(target_year, _circular_mean_doy([f[key].dayofyear for _, f in feats]))
    entry, season_start, peak_start, peak, peak_end, end = [avg_date(k) for k in ("entry","season_start","peak_start","peak","peak_end","end")]
    # 연말을 넘는 시즌 보정
    ordered = [entry, season_start, peak_start, peak, peak_end, end]
    for i in range(1, len(ordered)):
        if ordered[i] < ordered[i-1] - timedelta(days=180):
            ordered[i] = date(ordered[i].year + 1, ordered[i].month, ordered[i].day)
    entry, season_start, peak_start, peak, peak_end, end = ordered
    status, action = _date_status(entry, peak_start, peak, peak_end, end, today)
    remain = max(0, (end - today).days)
    season_days = max(1, (end - entry).days)
    progress = max(0, min(100, (today - entry).days / season_days * 100))
    seasonality = min(100, max(0, (peakm/(median+1e-9)-1)*50 + cv*80 + (12-active50)*5))
    return {
        "search_keyword": keyword, "target_year": target_year, "analysis_years": ", ".join(map(str, years)),
        "season_type_calculated": "제철형", "season_type_confidence": round(confidence, 1),
        "recommended_upload_date": (entry - timedelta(days=14)).isoformat(),
        "entry_date": entry.isoformat(), "season_start_date": season_start.isoformat(),
        "expected_peak_start_date": peak_start.isoformat(), "expected_peak_date": peak.isoformat(),
        "expected_peak_end_date": peak_end.isoformat(), "expected_end_date": end.isoformat(),
        "remaining_sales_days": remain, "season_progress": round(progress, 1),
        "seasonality_score": round(seasonality, 1), "judgement": status, "recommended_action": action,
        "yearly_peak_dates": ", ".join(f"{y}:{f['peak'].strftime('%m/%d')}" for y,f in feats),
        "last_analyzed_at": now,
    }

def analyze(raw: pd.DataFrame, category_map: Dict[str, List[str]], target_year: int, target_month: int, today: date | None = None) -> pd.DataFrame:
    reverse = {p: c for c, items in category_map.items() for p in items}
    rows = []
    for product in raw.columns:
        if product == ANCHOR or product not in reverse: continue
        try:
            r = analyze_keyword(raw, product, target_year, today)
        except ValueError:
            continue
        if r["season_type_calculated"] == "제철형":
            start = pd.to_datetime(r["entry_date"]).date(); end = pd.to_datetime(r["expected_end_date"]).date()
            ms = date(target_year, target_month, 1)
            me = date(target_year + (target_month == 12), 1 if target_month == 12 else target_month + 1, 1) - timedelta(days=1)
            if min(end, me) < max(start, ms): continue
        rows.append({"카테고리": reverse[product], "품목": product, **r})
    out = pd.DataFrame(rows)
    if out.empty: return out
    return out.sort_values(["카테고리","seasonality_score"], ascending=[True,False]).reset_index(drop=True)

def to_excel(results: pd.DataFrame, raw: pd.DataFrame) -> bytes:
    bio = BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as w:
        results.to_excel(w, index=False, sheet_name="분석결과")
        raw.to_excel(w, sheet_name="일별원자료")
    return bio.getvalue()
