# -*- coding: utf-8 -*-
from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Optional
import sqlite3
import shutil
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".marketscout"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "market_scout.db"
MASTER_CSV = BASE_DIR / "item_master.csv"
DEFAULT_DB = BASE_DIR / "default_market_scout.db"
_SCHEMA_READY = False

def connect(read_only: bool=False):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 새 설치에서는 프로젝트에 포함된 기본 DB를 사용자 데이터 폴더로 복사합니다.
    if not DB_PATH.exists() and DEFAULT_DB.exists():
        shutil.copy2(DEFAULT_DB, DB_PATH)
    if read_only and DB_PATH.exists():
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3)
    else:
        con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con

def ensure_schema(force: bool = False):
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with connect() as con:
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        con.executescript("""
        CREATE TABLE IF NOT EXISTS item_names(
          name_id INTEGER PRIMARY KEY AUTOINCREMENT,
          category TEXT NOT NULL, representative_name TEXT NOT NULL,
          search_name TEXT NOT NULL UNIQUE, name_type TEXT,
          season_type_initial TEXT DEFAULT '미정', active INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_item_search ON item_names(search_name);
        CREATE INDEX IF NOT EXISTS idx_item_rep ON item_names(representative_name);
        CREATE TABLE IF NOT EXISTS photo_guides(
          search_name TEXT PRIMARY KEY,
          same_variety_names TEXT DEFAULT '',
          similar_outer_varieties TEXT DEFAULT '',
          similar_cut_varieties TEXT DEFAULT '',
          skin_color TEXT DEFAULT '',
          flesh_color TEXT DEFAULT '',
          substitute_photo_allowed TEXT DEFAULT '조건부 가능',
          substitute_photo_notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS season_signals(
          search_keyword TEXT NOT NULL, target_year INTEGER NOT NULL,
          signal_type TEXT NOT NULL, signal_date TEXT NOT NULL,
          source_note TEXT DEFAULT '', updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          PRIMARY KEY(search_keyword,target_year)
        );
        CREATE TABLE IF NOT EXISTS analysis_results(
          search_keyword TEXT NOT NULL, target_year INTEGER NOT NULL,
          payload_json TEXT NOT NULL, last_analyzed_at TEXT,
          PRIMARY KEY(search_keyword,target_year)
        );
        CREATE TABLE IF NOT EXISTS trend_cache(
          keyword TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
          series_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS collection_queue(
          keyword TEXT PRIMARY KEY,
          category TEXT,
          representative_name TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_queue_status ON collection_queue(status);
        CREATE TABLE IF NOT EXISTS api_call_log(
          log_id INTEGER PRIMARY KEY AUTOINCREMENT,
          called_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          keyword_count INTEGER NOT NULL,
          keywords TEXT NOT NULL,
          result TEXT NOT NULL,
          detail TEXT
        );
        """)
    if MASTER_CSV.exists(): import_master_if_empty()
    _SCHEMA_READY = True

def _read_csv(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig","cp949","utf-8"):
        try: return pd.read_csv(path, encoding=enc, dtype=str).fillna("")
        except UnicodeDecodeError: pass
    return pd.read_csv(path, dtype=str).fillna("")

def import_master_if_empty():
    # CSV 마스터를 DB에 동기화합니다. 기존 DB 데이터는 보존하고 새 행만 추가합니다.
    df = _read_csv(MASTER_CSV)
    required = {"1분류","2분류","3분류_정규화"}
    if not required.issubset(df.columns):
        raise RuntimeError(f"마스터 CSV 열이 다릅니다. 필요: {sorted(required)}")
    rows=[]; guides=[]
    for _,r in df.iterrows():
        name=(r.get("3분류_정규화") or r.get("3분류") or "").strip()
        if not name or str(r.get("검색상태", "사용")).strip() not in ("", "사용"): continue
        row=(r["1분류"].strip(), r["2분류"].strip(), name, r.get("3분류_유형","").strip(), "미정", 1)
        rows.append(row)
        guides.append((name, r.get("같은품종_동일유통명","").strip(), r.get("겉모양_유사품종","").strip(),
                       r.get("단면_유사품종","").strip(), r.get("겉색","").strip(), r.get("과육색","").strip(),
                       r.get("사진대체_가능여부","조건부 가능").strip() or "조건부 가능",
                       r.get("사진대체_주의사항","").strip()))
        if name == "마늘종":
            rows.append((row[0], row[1], "마늘쫑", "별칭", row[4], row[5]))
    with connect() as con:
        con.executemany("INSERT OR IGNORE INTO item_names(category,representative_name,search_name,name_type,season_type_initial,active) VALUES(?,?,?,?,?,?)", rows)
        con.executemany('''INSERT INTO photo_guides(search_name,same_variety_names,similar_outer_varieties,similar_cut_varieties,skin_color,flesh_color,substitute_photo_allowed,substitute_photo_notes)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(search_name) DO UPDATE SET
          same_variety_names=CASE WHEN excluded.same_variety_names!='' THEN excluded.same_variety_names ELSE photo_guides.same_variety_names END,
          similar_outer_varieties=CASE WHEN excluded.similar_outer_varieties!='' THEN excluded.similar_outer_varieties ELSE photo_guides.similar_outer_varieties END,
          similar_cut_varieties=CASE WHEN excluded.similar_cut_varieties!='' THEN excluded.similar_cut_varieties ELSE photo_guides.similar_cut_varieties END,
          skin_color=CASE WHEN excluded.skin_color!='' THEN excluded.skin_color ELSE photo_guides.skin_color END,
          flesh_color=CASE WHEN excluded.flesh_color!='' THEN excluded.flesh_color ELSE photo_guides.flesh_color END,
          substitute_photo_allowed=excluded.substitute_photo_allowed,
          substitute_photo_notes=CASE WHEN excluded.substitute_photo_notes!='' THEN excluded.substitute_photo_notes ELSE photo_guides.substitute_photo_notes END''', guides)

def load_database_df():
    with connect(read_only=True) as con:
        return pd.read_sql_query("SELECT name_id,category AS 대분류,representative_name AS 기준품목,search_name AS 세부품목,name_type AS 이름유형,season_type_initial AS 초기판매유형 FROM item_names WHERE active=1 ORDER BY category,representative_name,search_name",con)

def category_map() -> Dict[str,List[str]]:
    df=load_database_df(); return {k:g["세부품목"].drop_duplicates().tolist() for k,g in df.groupby("대분류",sort=False)}

def categories() -> List[str]:
    with connect(read_only=True) as con: return [r[0] for r in con.execute("SELECT DISTINCT category FROM item_names ORDER BY category")]

def search_items(query: str):
    q=query.strip()
    if not q:return pd.DataFrame()
    with connect(read_only=True) as con:
        return pd.read_sql_query("""SELECT name_id,search_name AS 검색명,name_type AS 이름유형,representative_name AS 대표품목,season_type_initial AS 초기판매유형,category AS 카테고리 FROM item_names WHERE active=1 AND (search_name LIKE ? OR representative_name LIKE ?) ORDER BY CASE WHEN search_name=? THEN 0 WHEN representative_name=? THEN 1 ELSE 2 END,LENGTH(search_name) LIMIT 100""",con,params=[f"%{q}%",f"%{q}%",q,q])

def get_item_names(representative_name: str) -> List[str]:
    with connect(read_only=True) as con:return [r[0] for r in con.execute("SELECT search_name FROM item_names WHERE representative_name=? AND active=1 ORDER BY search_name",(representative_name,))]

def get_photo_guide(search_name: str, representative_name: str = "") -> dict:
    with connect(read_only=True) as con:
        row=con.execute("SELECT * FROM photo_guides WHERE search_name=?",(search_name,)).fetchone()
        names=[r[0] for r in con.execute("SELECT search_name FROM item_names WHERE representative_name=? AND active=1 ORDER BY search_name",(representative_name,))] if representative_name else []
    data=dict(row) if row else {}
    if not data.get("same_variety_names") and names:
        data["same_variety_names"]=', '.join(names[:30])
    data.setdefault("similar_outer_varieties", "확인 필요")
    data.setdefault("similar_cut_varieties", "확인 필요")
    data.setdefault("skin_color", "확인 필요")
    data.setdefault("flesh_color", "확인 필요")
    data.setdefault("substitute_photo_allowed", "조건부 가능")
    data.setdefault("substitute_photo_notes", "실제 판매 품목과 겉모양·단면·색이 일치하는지 확인 후 사용")
    return data

def add_item(category: str, representative_name: str, search_name: Optional[str]=None, season_type: str="미정", name_type: str="대표명") -> int:
    search_name=(search_name or representative_name).strip(); representative_name=representative_name.strip()
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO item_names(category,representative_name,search_name,name_type,season_type_initial,active) VALUES(?,?,?,?,?,1)",(category,representative_name,search_name,name_type,season_type))
        return int(con.execute("SELECT name_id FROM item_names WHERE search_name=?",(search_name,)).fetchone()[0])


def update_default_master_assets() -> dict:
    """현재 DB의 활성 품목을 CSV 마스터와 깨끗한 새 설치용 기본 DB로 내보냅니다.

    프로젝트 폴더가 쓰기 가능한 환경에서는 item_master.csv와
    default_market_scout.db를 즉시 교체합니다. Streamlit Cloud처럼 배포 파일이
    일시적일 수 있는 환경에서도 다운로드할 수 있도록 두 파일의 bytes를 반환합니다.
    """
    import os
    import tempfile

    ensure_schema()
    master = _read_csv(MASTER_CSV) if MASTER_CSV.exists() else pd.DataFrame()
    columns = [
        "1분류", "2분류", "3분류", "3분류_정규화", "3분류_유형",
        "기존_세부분류", "검색상태", "출처URL", "메모", "차수",
        "같은품종_동일유통명", "겉모양_유사품종", "단면_유사품종",
        "겉색", "과육색", "사진대체_가능여부", "사진대체_주의사항",
    ]
    for col in columns:
        if col not in master.columns:
            master[col] = ""
    master = master[columns].fillna("")

    with connect(read_only=True) as con:
        items = pd.read_sql_query(
            """SELECT i.category, i.representative_name, i.search_name,
                      COALESCE(i.name_type,'') AS name_type,
                      COALESCE(i.season_type_initial,'미정') AS season_type_initial,
                      COALESCE(g.same_variety_names,'') AS same_variety_names,
                      COALESCE(g.similar_outer_varieties,'') AS similar_outer_varieties,
                      COALESCE(g.similar_cut_varieties,'') AS similar_cut_varieties,
                      COALESCE(g.skin_color,'') AS skin_color,
                      COALESCE(g.flesh_color,'') AS flesh_color,
                      COALESCE(g.substitute_photo_allowed,'') AS substitute_photo_allowed,
                      COALESCE(g.substitute_photo_notes,'') AS substitute_photo_notes
               FROM item_names i
               LEFT JOIN photo_guides g ON g.search_name=i.search_name
               WHERE i.active=1
               ORDER BY i.category, i.representative_name, i.search_name""", con
        )

    by_name = {}
    for _, row in master.iterrows():
        name = str(row.get("3분류_정규화") or row.get("3분류") or "").strip()
        if name and name not in by_name:
            by_name[name] = {c: str(row.get(c, "") or "") for c in columns}

    added = 0
    updated = 0
    for _, r in items.iterrows():
        name = str(r["search_name"]).strip()
        existing = by_name.get(name)
        if existing is None:
            existing = {c: "" for c in columns}
            existing.update({
                "출처URL": "", "메모": "앱에서 추가 후 기본 DB 업데이트",
                "차수": "앱추가", "검색상태": "사용",
            })
            by_name[name] = existing
            added += 1
        else:
            updated += 1
        existing.update({
            "1분류": str(r["category"] or ""),
            "2분류": str(r["representative_name"] or name),
            "3분류": name,
            "3분류_정규화": name,
            "3분류_유형": str(r["name_type"] or existing.get("3분류_유형") or "대표명"),
            "기존_세부분류": existing.get("기존_세부분류") or str(r["category"] or ""),
            "검색상태": "사용",
        })
        photo_map = {
            "같은품종_동일유통명": "same_variety_names",
            "겉모양_유사품종": "similar_outer_varieties",
            "단면_유사품종": "similar_cut_varieties",
            "겉색": "skin_color",
            "과육색": "flesh_color",
            "사진대체_가능여부": "substitute_photo_allowed",
            "사진대체_주의사항": "substitute_photo_notes",
        }
        for csv_col, db_col in photo_map.items():
            value = str(r[db_col] or "").strip()
            if value:
                existing[csv_col] = value

    merged = pd.DataFrame(by_name.values(), columns=columns).fillna("")
    merged = merged.drop_duplicates(subset=["3분류_정규화"], keep="last")
    merged = merged.sort_values(["1분류", "2분류", "3분류_정규화"], kind="stable").reset_index(drop=True)
    csv_bytes = merged.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")

    csv_written = False
    try:
        MASTER_CSV.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="item_master_", suffix=".csv", dir=str(MASTER_CSV.parent))
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.write_bytes(csv_bytes)
        os.replace(tmp_path, MASTER_CSV)
        csv_written = True
    except OSError:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    fd, db_tmp_name = tempfile.mkstemp(prefix="default_market_scout_", suffix=".db")
    os.close(fd)
    db_tmp = Path(db_tmp_name)
    try:
        out = sqlite3.connect(db_tmp)
        try:
            out.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE item_names(
              name_id INTEGER PRIMARY KEY AUTOINCREMENT,
              category TEXT NOT NULL, representative_name TEXT NOT NULL,
              search_name TEXT NOT NULL UNIQUE, name_type TEXT,
              season_type_initial TEXT DEFAULT '미정', active INTEGER DEFAULT 1
            );
            CREATE INDEX idx_item_search ON item_names(search_name);
            CREATE INDEX idx_item_rep ON item_names(representative_name);
            CREATE TABLE photo_guides(
              search_name TEXT PRIMARY KEY, same_variety_names TEXT DEFAULT '',
              similar_outer_varieties TEXT DEFAULT '', similar_cut_varieties TEXT DEFAULT '',
              skin_color TEXT DEFAULT '', flesh_color TEXT DEFAULT '',
              substitute_photo_allowed TEXT DEFAULT '조건부 가능', substitute_photo_notes TEXT DEFAULT ''
            );
            CREATE TABLE analysis_results(
              search_keyword TEXT NOT NULL, target_year INTEGER NOT NULL, payload_json TEXT NOT NULL,
              last_analyzed_at TEXT, PRIMARY KEY(search_keyword,target_year)
            );
            CREATE TABLE trend_cache(
              keyword TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
              series_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE collection_queue(
              keyword TEXT PRIMARY KEY, category TEXT, representative_name TEXT,
              status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX idx_queue_status ON collection_queue(status);
            CREATE TABLE api_call_log(
              log_id INTEGER PRIMARY KEY AUTOINCREMENT,
              called_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
              keyword_count INTEGER NOT NULL, keywords TEXT NOT NULL, result TEXT NOT NULL, detail TEXT
            );
            """)
            item_rows = [tuple(x) for x in items[[
                "category", "representative_name", "search_name", "name_type", "season_type_initial"
            ]].itertuples(index=False, name=None)]
            out.executemany(
                "INSERT INTO item_names(category,representative_name,search_name,name_type,season_type_initial,active) VALUES(?,?,?,?,?,1)",
                item_rows,
            )
            guide_rows = [tuple(x) for x in items[[
                "search_name", "same_variety_names", "similar_outer_varieties",
                "similar_cut_varieties", "skin_color", "flesh_color",
                "substitute_photo_allowed", "substitute_photo_notes"
            ]].itertuples(index=False, name=None)]
            out.executemany(
                """INSERT INTO photo_guides(search_name,same_variety_names,similar_outer_varieties,
                   similar_cut_varieties,skin_color,flesh_color,substitute_photo_allowed,substitute_photo_notes)
                   VALUES(?,?,?,?,?,?,?,?)""", guide_rows
            )
            out.commit()
            check = out.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"새 기본 DB 무결성 검사 실패: {check}")
        finally:
            out.close()
        db_bytes = db_tmp.read_bytes()
        db_written = False
        try:
            DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
            fd2, local_tmp_name = tempfile.mkstemp(prefix="default_db_", suffix=".db", dir=str(DEFAULT_DB.parent))
            os.close(fd2)
            local_tmp = Path(local_tmp_name)
            local_tmp.write_bytes(db_bytes)
            os.replace(local_tmp, DEFAULT_DB)
            db_written = True
        except OSError:
            try:
                local_tmp.unlink()
            except Exception:
                pass
    finally:
        try:
            db_tmp.unlink()
        except FileNotFoundError:
            pass

    return {
        "item_count": int(len(items)),
        "csv_row_count": int(len(merged)),
        "added_to_csv": int(added),
        "updated_in_csv": int(updated),
        "csv_bytes": csv_bytes,
        "default_db_bytes": db_bytes,
        "csv_written": csv_written,
        "default_db_written": db_written,
    }

def save_season_signal(search_keyword: str, target_year: int, signal_type: str, signal_date, source_note: str = ""):
    ensure_schema()
    d = pd.Timestamp(signal_date).date().isoformat()
    with connect() as con:
        con.execute("""INSERT INTO season_signals(search_keyword,target_year,signal_type,signal_date,source_note,updated_at)
          VALUES(?,?,?,?,?,datetime('now','localtime'))
          ON CONFLICT(search_keyword,target_year) DO UPDATE SET signal_type=excluded.signal_type,signal_date=excluded.signal_date,source_note=excluded.source_note,updated_at=excluded.updated_at""",
          (search_keyword.strip(), int(target_year), signal_type.strip(), d, source_note.strip()))

def delete_season_signal(search_keyword: str, target_year: int):
    with connect() as con:
        con.execute("DELETE FROM season_signals WHERE search_keyword=? AND target_year=?",(search_keyword.strip(),int(target_year)))

def load_season_signals(target_year: Optional[int] = None) -> pd.DataFrame:
    ensure_schema()
    sql="SELECT search_keyword,target_year,signal_type,signal_date,source_note,updated_at FROM season_signals"
    params=[]
    if target_year is not None:
        sql += " WHERE target_year=?"; params=[int(target_year)]
    sql += " ORDER BY signal_date DESC,search_keyword"
    with connect(read_only=True) as con:
        return pd.read_sql_query(sql,con,params=params)

def apply_season_signals(df: pd.DataFrame, target_year: int) -> pd.DataFrame:
    """출하 신호를 이용해 3년 평균 일정을 최대 ±21일 보정하고 선점점수/오늘행동을 계산합니다."""
    if df is None or df.empty: return df
    out=df.copy(); signals=load_season_signals(target_year)
    signal_map={str(r.search_keyword):r for r in signals.itertuples()} if not signals.empty else {}
    date_cols=["exploration_start_date","photo_prepare_date","recommended_upload_date","ad_start_date","entry_date","season_start_date","expected_peak_start_date","expected_peak_date","expected_peak_end_date","gentle_decline_start_date","expected_end_date"]
    offsets=[]; types=[]; dates=[]; notes=[]
    expected_before={"첫 수확":14,"첫 출하":7,"본격 출하":0,"공판장 반입":3}
    for idx,r in out.iterrows():
        sig=signal_map.get(str(r.get("search_keyword",""))); offset=0
        if sig is not None and r.get("entry_date"):
            entry=pd.to_datetime(r.get("entry_date"),errors="coerce")
            actual=pd.to_datetime(sig.signal_date,errors="coerce")
            if pd.notna(entry) and pd.notna(actual):
                expected=entry-pd.Timedelta(days=expected_before.get(sig.signal_type,0))
                offset=max(-21,min(21,int((actual-expected).days)))
                for c in date_cols:
                    if c in out.columns and pd.notna(r.get(c)):
                        d=pd.to_datetime(r.get(c),errors="coerce")
                        if pd.notna(d): out.at[idx,c]=(d+pd.Timedelta(days=offset)).date().isoformat()
        offsets.append(offset); types.append(getattr(sig,"signal_type","") if sig else ""); dates.append(getattr(sig,"signal_date","") if sig else ""); notes.append(getattr(sig,"source_note","") if sig else "")
    out["year_adjust_days"]=offsets; out["season_signal_type"]=types; out["season_signal_date"]=dates; out["season_signal_note"]=notes
    today=pd.Timestamp.today().date()
    actions=[]; scores=[]
    for _,r in out.iterrows():
        def d(k):
            x=pd.to_datetime(r.get(k),errors="coerce"); return x.date() if pd.notna(x) else None
        exp,photo,upload,ad,entry,end=d("exploration_start_date"),d("photo_prepare_date"),d("recommended_upload_date"),d("ad_start_date"),d("entry_date"),d("expected_end_date")
        if r.get("season_type_calculated")=="사계절형": action="상시 운영"
        elif exp and today < exp: action="아직 대기"
        elif photo and today < photo: action="공급처 탐색"
        elif upload and today < upload: action="사진·상세페이지 준비"
        elif ad and today < ad: action="오늘 선등록"
        elif entry and today < entry: action="광고·가격 준비"
        elif end and today <= end: action="판매 운영"
        else: action="시즌 종료"
        actions.append(action)
        rise=float(r.get("rise_signal_score") or 50); repeat=float(r.get("season_type_confidence") or 0)
        proximity=50.0
        if entry:
            days=(entry-today).days; proximity=max(0,min(100,100-abs(days-21)*2.5))
        signal=100.0 if r.get("season_signal_date") else 0.0
        accel=max(0,min(100,50+float(r.get("recent_acceleration") or 0)*4))
        scores.append(round(rise*.30+repeat*.25+proximity*.20+signal*.15+accel*.10,1))
    out["today_action"]=actions; out["preemption_score"]=scores
    return out

def save_analysis(result: dict, *_args):
    import json
    with connect() as con:
        con.execute("INSERT INTO analysis_results(search_keyword,target_year,payload_json,last_analyzed_at) VALUES(?,?,?,?) ON CONFLICT(search_keyword,target_year) DO UPDATE SET payload_json=excluded.payload_json,last_analyzed_at=excluded.last_analyzed_at",(result["search_keyword"],int(result["target_year"]),json.dumps(result,ensure_ascii=False),result.get("last_analyzed_at")))

def load_analysis(keyword: Optional[str]=None,target_year: Optional[int]=None):
    import json
    where=[]; params=[]
    if keyword: where.append("search_keyword=?");params.append(keyword)
    if target_year: where.append("target_year=?");params.append(target_year)
    sql="SELECT payload_json FROM analysis_results"+(" WHERE "+" AND ".join(where) if where else "")+" ORDER BY last_analyzed_at DESC"
    with connect(read_only=True) as con: rows=con.execute(sql,params).fetchall()
    return pd.DataFrame([json.loads(r[0]) for r in rows])

# ---------------- persistent trend cache / backup ----------------
def save_raw_series(keyword: str, series: pd.Series, start_date: str, end_date: str):
    import json
    payload = {pd.Timestamp(k).date().isoformat(): float(v) for k, v in series.dropna().items()}
    with connect() as con:
        con.execute('''INSERT INTO trend_cache(keyword,start_date,end_date,series_json,updated_at)
            VALUES(?,?,?,?,datetime('now','localtime'))
            ON CONFLICT(keyword) DO UPDATE SET start_date=excluded.start_date,end_date=excluded.end_date,
            series_json=excluded.series_json,updated_at=excluded.updated_at''',
            (keyword,start_date,end_date,json.dumps(payload,ensure_ascii=False)))

def load_raw_series(keyword: str, start_date: str | None=None, end_date: str | None=None):
    import json
    with connect(read_only=True) as con:
        row=con.execute('SELECT start_date,end_date,series_json,updated_at FROM trend_cache WHERE keyword=?',(keyword,)).fetchone()
    if not row: return None
    if start_date and row['start_date'] > start_date: return None
    if end_date and row['end_date'] < end_date: return None
    data=json.loads(row['series_json'])
    s=pd.Series(data,dtype=float,name=keyword); s.index=pd.to_datetime(s.index); s=s.sort_index()
    return s

def cached_keywords() -> pd.DataFrame:
    with connect(read_only=True) as con:
        return pd.read_sql_query('SELECT keyword AS 품목,start_date AS 시작일,end_date AS 종료일,updated_at AS 저장일시 FROM trend_cache ORDER BY updated_at DESC',con)

def backup_database_bytes() -> bytes:
    # SQLite online backup creates a consistent snapshot even while app is running.
    import tempfile, os
    fd,tmp=tempfile.mkstemp(suffix='.db'); os.close(fd)
    try:
        src=connect(); dst=sqlite3.connect(tmp)
        try: src.backup(dst)
        finally: dst.close(); src.close()
        return Path(tmp).read_bytes()
    finally:
        try: Path(tmp).unlink()
        except FileNotFoundError: pass

def restore_database_bytes(data: bytes):
    import tempfile, os
    fd,tmp=tempfile.mkstemp(suffix='.db'); os.close(fd)
    try:
        Path(tmp).write_bytes(data)
        test=sqlite3.connect(tmp)
        try:
            ok=test.execute('PRAGMA integrity_check').fetchone()[0]
            if ok != 'ok': raise RuntimeError(f'DB 무결성 검사 실패: {ok}')
        finally: test.close()
        DB_PATH.parent.mkdir(parents=True,exist_ok=True)
        Path(tmp).replace(DB_PATH)
    finally:
        try: Path(tmp).unlink()
        except FileNotFoundError: pass
    ensure_schema(force=True)

# ---------------- bulk collection resume / logs ----------------
def ensure_bulk_schema():
    # initialize_database() runs once at app startup. Avoid schema writes on every read.
    if not _SCHEMA_READY:
        ensure_schema()

def enqueue_keywords(rows):
    ensure_bulk_schema()
    with connect() as con:
        con.executemany('''INSERT INTO collection_queue(keyword,category,representative_name,status,updated_at)
          VALUES(?,?,?,'pending',datetime('now','localtime'))
          ON CONFLICT(keyword) DO UPDATE SET category=excluded.category,
          representative_name=excluded.representative_name,
          status=CASE WHEN collection_queue.status='completed' THEN 'completed' ELSE 'pending' END,
          updated_at=datetime('now','localtime')''', rows)
    sync_queue_with_cache()

def sync_queue_with_cache() -> int:
    """Synchronize the queue with already cached keywords.

    Called only after queue changes or by an explicit repair action. It is never
    called from dashboard read functions, avoiding write locks on every rerun.
    Returns the number of queue rows changed. Older/restored DBs without the
    expected cache table are safely ignored instead of crashing the app.
    """
    ensure_bulk_schema()
    try:
        with connect() as con:
            table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trend_cache'"
            ).fetchone()
            if not table:
                return 0
            columns = {r[1] for r in con.execute("PRAGMA table_info(trend_cache)").fetchall()}
            if "keyword" not in columns:
                return 0
            cur = con.execute("""UPDATE collection_queue
                SET status='completed', last_error=NULL,
                    updated_at=datetime('now','localtime')
                WHERE status!='completed'
                  AND EXISTS (
                    SELECT 1 FROM trend_cache t
                    WHERE t.keyword=collection_queue.keyword
                  )""")
            return max(int(cur.rowcount or 0), 0)
    except sqlite3.OperationalError:
        # A simultaneous item commit may briefly own the write lock. The next
        # explicit synchronization will catch up; cached data itself is safe.
        return 0

def reset_failed_queue():
    ensure_bulk_schema()
    with connect() as con:
        con.execute("UPDATE collection_queue SET status='pending',last_error=NULL,updated_at=datetime('now','localtime') WHERE status='failed'")

def clear_pending_queue():
    ensure_bulk_schema()
    with connect() as con:
        con.execute("DELETE FROM collection_queue WHERE status!='completed'")

def next_pending_keywords(limit: int=5):
    # Read-only: never run UPDATE during normal screen rendering/collection loops.
    ensure_bulk_schema()
    with connect(read_only=True) as con:
        return pd.read_sql_query('''SELECT keyword,category,representative_name FROM collection_queue
          WHERE status='pending' ORDER BY rowid LIMIT ?''', con, params=[int(limit)])

def mark_queue_completed(keyword: str):
    ensure_bulk_schema()
    with connect() as con:
        con.execute("UPDATE collection_queue SET status='completed',last_error=NULL,updated_at=datetime('now','localtime') WHERE keyword=?",(keyword,))

def mark_queue_failed(keyword: str, error: str):
    ensure_bulk_schema()
    with connect() as con:
        con.execute("UPDATE collection_queue SET status='failed',attempts=attempts+1,last_error=?,updated_at=datetime('now','localtime') WHERE keyword=?",(str(error)[:1000],keyword))

def queue_status_df():
    # Read-only so opening a tab cannot lock the database.
    ensure_bulk_schema()
    with connect(read_only=True) as con:
        return pd.read_sql_query('''SELECT keyword AS 품목,category AS 카테고리,representative_name AS 대표품목,
          status AS 상태,attempts AS 시도횟수,last_error AS 최근오류,updated_at AS 갱신일시
          FROM collection_queue ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END, updated_at DESC''',con)

def queue_counts():
    # This function is called on every Streamlit rerun, therefore it must remain
    # strictly read-only. Completed items are marked immediately after save.
    ensure_bulk_schema()
    with connect(read_only=True) as con:
        rows=con.execute("SELECT status,COUNT(*) FROM collection_queue GROUP BY status").fetchall()
    d={'pending':0,'completed':0,'failed':0}
    d.update({r[0]:r[1] for r in rows}); d['total']=sum(d.values()); return d

def log_api_call(keywords, result: str, detail: str=''):
    ensure_bulk_schema()
    with connect() as con:
        con.execute('INSERT INTO api_call_log(keyword_count,keywords,result,detail) VALUES(?,?,?,?)',
                    (len(keywords),', '.join(keywords),result,str(detail)[:1500]))

def api_log_df(limit: int=200):
    ensure_bulk_schema()
    with connect(read_only=True) as con:
        return pd.read_sql_query('''SELECT called_at AS 호출일시,keyword_count AS 품목수,keywords AS 품목,result AS 결과,detail AS 상세
          FROM api_call_log ORDER BY log_id DESC LIMIT ?''',con,params=[int(limit)])

def today_api_calls() -> int:
    ensure_bulk_schema()
    with connect(read_only=True) as con:
        return int(con.execute("SELECT COUNT(*) FROM api_call_log WHERE date(called_at)=date('now','localtime')").fetchone()[0])



def reset_keywords_for_refresh(keywords) -> dict:
    """Delete cached raw data and analysis for selected keywords, then queue them again."""
    cleaned = []
    seen = set()
    for value in keywords or []:
        kw = str(value).strip()
        if kw and kw not in seen:
            seen.add(kw)
            cleaned.append(kw)
    if not cleaned:
        return {"requested": 0, "cache_deleted": 0, "analysis_deleted": 0, "queued": 0}

    ensure_bulk_schema()
    placeholders = ",".join("?" for _ in cleaned)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cache_deleted = con.execute(
            f"DELETE FROM trend_cache WHERE keyword IN ({placeholders})", cleaned
        ).rowcount or 0
        analysis_deleted = con.execute(
            f"DELETE FROM analysis_results WHERE search_keyword IN ({placeholders})", cleaned
        ).rowcount or 0

        sql = """INSERT INTO collection_queue(
                    keyword, category, representative_name, status,
                    attempts, last_error, updated_at
                 ) VALUES(?,?,?,'pending',0,NULL,datetime('now','localtime'))
                 ON CONFLICT(keyword) DO UPDATE SET
                   status='pending', attempts=0, last_error=NULL,
                   category=CASE WHEN excluded.category!='' THEN excluded.category ELSE collection_queue.category END,
                   representative_name=CASE WHEN excluded.representative_name!='' THEN excluded.representative_name ELSE collection_queue.representative_name END,
                   updated_at=datetime('now','localtime')"""
        for kw in cleaned:
            item = con.execute(
                "SELECT category, representative_name FROM item_names WHERE search_name=? LIMIT 1",
                (kw,),
            ).fetchone()
            category = item["category"] if item else ""
            representative = item["representative_name"] if item else kw
            con.execute(sql, (kw, category, representative))

    return {
        "requested": len(cleaned),
        "cache_deleted": int(cache_deleted),
        "analysis_deleted": int(analysis_deleted),
        "queued": len(cleaned),
    }


def reset_categories_for_refresh(category_names, representative_only: bool = True) -> dict:
    """Reset all active items in selected categories for fresh collection."""
    cats = [str(x).strip() for x in (category_names or []) if str(x).strip()]
    if not cats:
        return {"requested": 0, "cache_deleted": 0, "analysis_deleted": 0, "queued": 0}

    placeholders = ",".join("?" for _ in cats)
    with connect(read_only=True) as con:
        if representative_only:
            rows = con.execute(
                f"SELECT DISTINCT representative_name FROM item_names WHERE active=1 AND category IN ({placeholders}) ORDER BY representative_name",
                cats,
            ).fetchall()
        else:
            rows = con.execute(
                f"SELECT DISTINCT search_name FROM item_names WHERE active=1 AND category IN ({placeholders}) ORDER BY search_name",
                cats,
            ).fetchall()
    return reset_keywords_for_refresh([r[0] for r in rows])


def reset_all_collection_data() -> dict:
    """Delete all raw/analysis data and return the existing queue to pending."""
    ensure_bulk_schema()
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cache_count = con.execute("SELECT COUNT(*) FROM trend_cache").fetchone()[0]
        analysis_count = con.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0]
        queue_count = con.execute("SELECT COUNT(*) FROM collection_queue").fetchone()[0]
        con.execute("DELETE FROM trend_cache")
        con.execute("DELETE FROM analysis_results")
        con.execute(
            "UPDATE collection_queue SET status='pending', attempts=0, last_error=NULL, updated_at=datetime('now','localtime')"
        )
    return {
        "cache_deleted": int(cache_count),
        "analysis_deleted": int(analysis_count),
        "queued": int(queue_count),
    }

def initialize_database():
    ensure_schema()
    return True
