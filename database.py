# -*- coding: utf-8 -*-
from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Optional
import sqlite3
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".marketscout"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "market_scout.db"
MASTER_CSV = BASE_DIR / "item_master.csv"

def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def ensure_schema():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS item_names(
          name_id INTEGER PRIMARY KEY AUTOINCREMENT,
          category TEXT NOT NULL, representative_name TEXT NOT NULL,
          search_name TEXT NOT NULL UNIQUE, name_type TEXT,
          season_type_initial TEXT DEFAULT '미정', active INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_item_search ON item_names(search_name);
        CREATE INDEX IF NOT EXISTS idx_item_rep ON item_names(representative_name);
        CREATE TABLE IF NOT EXISTS analysis_results(
          search_keyword TEXT NOT NULL, target_year INTEGER NOT NULL,
          payload_json TEXT NOT NULL, last_analyzed_at TEXT,
          PRIMARY KEY(search_keyword,target_year)
        );
        """)
    if MASTER_CSV.exists(): import_master_if_empty()

def _read_csv(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig","cp949","utf-8"):
        try: return pd.read_csv(path, encoding=enc, dtype=str).fillna("")
        except UnicodeDecodeError: pass
    return pd.read_csv(path, dtype=str).fillna("")

def import_master_if_empty():
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM item_names").fetchone()[0]
        if n: return
    df = _read_csv(MASTER_CSV)
    required = {"1분류","2분류","3분류_정규화"}
    if not required.issubset(df.columns):
        raise RuntimeError(f"마스터 CSV 열이 다릅니다. 필요: {sorted(required)}")
    rows=[]
    for _,r in df.iterrows():
        name=(r.get("3분류_정규화") or r.get("3분류") or "").strip()
        if not name or str(r.get("검색상태", "사용")).strip() not in ("", "사용"): continue
        row=(r["1분류"].strip(), r["2분류"].strip(), name, r.get("3분류_유형","").strip(), "미정", 1)
        rows.append(row)
        # 실제 판매자가 자주 쓰는 표기 별칭
        if name == "마늘종":
            rows.append((row[0], row[1], "마늘쫑", "별칭", row[4], row[5]))
    with connect() as con:
        con.executemany("INSERT OR IGNORE INTO item_names(category,representative_name,search_name,name_type,season_type_initial,active) VALUES(?,?,?,?,?,?)", rows)

def load_database_df():
    with connect() as con:
        return pd.read_sql_query("SELECT name_id,category AS 대분류,representative_name AS 기준품목,search_name AS 세부품목,name_type AS 이름유형,season_type_initial AS 초기판매유형 FROM item_names WHERE active=1 ORDER BY category,representative_name,search_name",con)

def category_map() -> Dict[str,List[str]]:
    df=load_database_df(); return {k:g["세부품목"].drop_duplicates().tolist() for k,g in df.groupby("대분류",sort=False)}

def categories() -> List[str]:
    with connect() as con: return [r[0] for r in con.execute("SELECT DISTINCT category FROM item_names ORDER BY category")]

def search_items(query: str):
    q=query.strip()
    if not q:return pd.DataFrame()
    with connect() as con:
        return pd.read_sql_query("""SELECT name_id,search_name AS 검색명,name_type AS 이름유형,representative_name AS 대표품목,season_type_initial AS 초기판매유형,category AS 카테고리 FROM item_names WHERE active=1 AND (search_name LIKE ? OR representative_name LIKE ?) ORDER BY CASE WHEN search_name=? THEN 0 WHEN representative_name=? THEN 1 ELSE 2 END,LENGTH(search_name) LIMIT 100""",con,params=[f"%{q}%",f"%{q}%",q,q])

def get_item_names(representative_name: str) -> List[str]:
    with connect() as con:return [r[0] for r in con.execute("SELECT search_name FROM item_names WHERE representative_name=? AND active=1 ORDER BY search_name",(representative_name,))]

def add_item(category: str, representative_name: str, search_name: Optional[str]=None, season_type: str="미정", name_type: str="대표명") -> int:
    search_name=(search_name or representative_name).strip(); representative_name=representative_name.strip()
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO item_names(category,representative_name,search_name,name_type,season_type_initial,active) VALUES(?,?,?,?,?,1)",(category,representative_name,search_name,name_type,season_type))
        return int(con.execute("SELECT name_id FROM item_names WHERE search_name=?",(search_name,)).fetchone()[0])

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
    with connect() as con: rows=con.execute(sql,params).fetchall()
    return pd.DataFrame([json.loads(r[0]) for r in rows])

ensure_schema()
