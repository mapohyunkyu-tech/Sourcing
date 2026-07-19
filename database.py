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
    # Streamlit reruns can overlap briefly. A generous timeout prevents transient
    # "database is locked" errors while another request is committing one item.
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
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

# ---------------- persistent trend cache / backup ----------------
def save_raw_series(keyword: str, series: pd.Series, start_date: str, end_date: str):
    import json
    payload = {pd.Timestamp(k).date().isoformat(): float(v) for k, v in series.dropna().items()}
    with connect() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS trend_cache(
            keyword TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            series_json TEXT NOT NULL, updated_at TEXT NOT NULL
        )''')
        con.execute('''INSERT INTO trend_cache(keyword,start_date,end_date,series_json,updated_at)
            VALUES(?,?,?,?,datetime('now','localtime'))
            ON CONFLICT(keyword) DO UPDATE SET start_date=excluded.start_date,end_date=excluded.end_date,
            series_json=excluded.series_json,updated_at=excluded.updated_at''',
            (keyword,start_date,end_date,json.dumps(payload,ensure_ascii=False)))

def load_raw_series(keyword: str, start_date: str | None=None, end_date: str | None=None):
    import json
    with connect() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS trend_cache(
            keyword TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            series_json TEXT NOT NULL, updated_at TEXT NOT NULL
        )''')
        row=con.execute('SELECT start_date,end_date,series_json,updated_at FROM trend_cache WHERE keyword=?',(keyword,)).fetchone()
    if not row: return None
    if start_date and row['start_date'] > start_date: return None
    if end_date and row['end_date'] < end_date: return None
    data=json.loads(row['series_json'])
    s=pd.Series(data,dtype=float,name=keyword); s.index=pd.to_datetime(s.index); s=s.sort_index()
    return s

def cached_keywords() -> pd.DataFrame:
    with connect() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS trend_cache(
            keyword TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            series_json TEXT NOT NULL, updated_at TEXT NOT NULL
        )''')
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
    ensure_schema()

# ---------------- bulk collection resume / logs ----------------
def ensure_bulk_schema():
    with connect() as con:
        con.executescript('''
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
        ''')

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
    with connect() as con:
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
    with connect() as con:
        return pd.read_sql_query('''SELECT keyword AS 품목,category AS 카테고리,representative_name AS 대표품목,
          status AS 상태,attempts AS 시도횟수,last_error AS 최근오류,updated_at AS 갱신일시
          FROM collection_queue ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END, updated_at DESC''',con)

def queue_counts():
    # This function is called on every Streamlit rerun, therefore it must remain
    # strictly read-only. Completed items are marked immediately after save.
    ensure_bulk_schema()
    with connect() as con:
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
    with connect() as con:
        return pd.read_sql_query('''SELECT called_at AS 호출일시,keyword_count AS 품목수,keywords AS 품목,result AS 결과,detail AS 상세
          FROM api_call_log ORDER BY log_id DESC LIMIT ?''',con,params=[int(limit)])

def today_api_calls() -> int:
    ensure_bulk_schema()
    with connect() as con:
        return int(con.execute("SELECT COUNT(*) FROM api_call_log WHERE date(called_at)=date('now','localtime')").fetchone()[0])

ensure_bulk_schema()
