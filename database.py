# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import sqlite3
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "agri_fish_item_master_v1.db"
SEED_CSV = BASE_DIR / "item_master_seed_v1.csv"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def ensure_schema() -> None:
    if not DB_PATH.exists():
        raise RuntimeError(f"품목 DB 파일이 없습니다: {DB_PATH}")
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS keyword_analysis_results (
            analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            name_id INTEGER,
            search_keyword TEXT NOT NULL,
            target_year INTEGER NOT NULL,
            analysis_years TEXT NOT NULL,
            season_type_calculated TEXT NOT NULL,
            season_type_confidence REAL,
            recommended_upload_date TEXT,
            entry_date TEXT,
            expected_peak_date TEXT,
            expected_end_date TEXT,
            seasonality_score REAL,
            offseason_retention_rate REAL,
            peak_concentration_rate REAL,
            year_consistency_rate REAL,
            recent_30d_change REAL,
            judgement TEXT,
            last_analyzed_at TEXT NOT NULL,
            UNIQUE(search_keyword, target_year),
            FOREIGN KEY(item_id) REFERENCES items(item_id) ON DELETE CASCADE,
            FOREIGN KEY(name_id) REFERENCES item_names(name_id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_keyword_analysis_item ON keyword_analysis_results(item_id);
        CREATE INDEX IF NOT EXISTS idx_keyword_analysis_keyword ON keyword_analysis_results(search_keyword);
        CREATE INDEX IF NOT EXISTS idx_item_names_sub_name ON item_names(sub_name);
        """)


ensure_schema()


def load_database_df() -> pd.DataFrame:
    sql = """
    SELECT i.item_id, n.name_id, c.category_name AS 대분류,
           i.representative_name AS 기준품목, n.sub_name AS 세부품목,
           n.name_type AS 이름유형, i.season_type_initial AS 초기판매유형,
           i.status AS 상태, CASE WHEN n.name_type='대표명' THEN '대표품목' ELSE '확장명' END AS 구분
    FROM items i
    JOIN item_names n ON n.item_id=i.item_id AND n.include_in_search=1
    LEFT JOIN item_categories ic ON ic.item_id=i.item_id AND ic.is_primary=1
    LEFT JOIN categories c ON c.category_id=ic.category_id
    WHERE i.status='active'
    ORDER BY c.category_id, i.item_id, n.name_id
    """
    with connect() as con:
        return pd.read_sql_query(sql, con)


def load_master_df() -> pd.DataFrame:
    return load_database_df()


def expected_counts() -> Dict[str, int]:
    df = load_database_df()
    return df.groupby("대분류", sort=False)["세부품목"].nunique().to_dict()


EXPECTED_COUNTS = expected_counts()


def category_map(include_aliases: bool = True) -> Dict[str, List[str]]:
    df = load_database_df()
    if not include_aliases:
        df = df[df["이름유형"] == "대표명"]
    return {k: g["세부품목"].drop_duplicates().tolist() for k, g in df.groupby("대분류", sort=False)}


def categories() -> List[str]:
    with connect() as con:
        rows = con.execute("SELECT category_name FROM categories ORDER BY category_id").fetchall()
    return [r[0] for r in rows]


def search_items(query: str) -> pd.DataFrame:
    q = query.strip()
    if not q:
        return pd.DataFrame()
    sql = """
    SELECT i.item_id, n.name_id, n.sub_name AS 검색명, n.name_type AS 이름유형,
           i.representative_name AS 대표품목, i.season_type_initial AS 초기판매유형,
           c.category_name AS 카테고리,
           CASE WHEN n.sub_name=? THEN 0 WHEN i.representative_name=? THEN 1 ELSE 2 END AS 정렬
    FROM item_names n
    JOIN items i ON i.item_id=n.item_id
    LEFT JOIN item_categories ic ON ic.item_id=i.item_id AND ic.is_primary=1
    LEFT JOIN categories c ON c.category_id=ic.category_id
    WHERE i.status='active' AND n.include_in_search=1
      AND (n.sub_name LIKE ? OR i.representative_name LIKE ?)
    ORDER BY 정렬, LENGTH(n.sub_name), n.name_id
    """
    like = f"%{q}%"
    with connect() as con:
        return pd.read_sql_query(sql, con, params=[q, q, like, like])


def get_item_names(item_id: int) -> List[str]:
    with connect() as con:
        rows = con.execute(
            "SELECT sub_name FROM item_names WHERE item_id=? AND include_in_search=1 ORDER BY name_id", (item_id,)
        ).fetchall()
    return [r[0] for r in rows]


def get_item_details(item_id: int) -> dict:
    with connect() as con:
        item = con.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        if not item:
            return {}
        names = con.execute("SELECT name_id,sub_name,name_type,include_in_search,notes FROM item_names WHERE item_id=? ORDER BY name_id", (item_id,)).fetchall()
        cats = con.execute("""
            SELECT c.category_name, ic.is_primary FROM item_categories ic
            JOIN categories c ON c.category_id=ic.category_id WHERE ic.item_id=? ORDER BY ic.is_primary DESC,c.category_id
        """, (item_id,)).fetchall()
        forms = con.execute("""
            SELECT pf.form_name FROM item_product_forms ipf JOIN product_forms pf ON pf.product_form_id=ipf.product_form_id
            WHERE ipf.item_id=? ORDER BY pf.product_form_id
        """, (item_id,)).fetchall()
        rules = con.execute("SELECT include_keywords,exclude_keywords,rule_notes FROM search_rules WHERE item_id=? ORDER BY rule_id", (item_id,)).fetchall()
    return {"item": dict(item), "names": [dict(x) for x in names], "categories": [dict(x) for x in cats],
            "forms": [x[0] for x in forms], "rules": [dict(x) for x in rules]}


def keyword_plan(item_id: int, selected_name: Optional[str] = None) -> dict:
    """데이터랩 그룹 계획. 대표명+별칭을 한 그룹으로 묶고 제외어는 오염 경고로 반환한다."""
    details = get_item_details(item_id)
    if not details:
        raise ValueError("품목을 찾을 수 없습니다.")
    rep = details["item"]["representative_name"]
    enabled = [x["sub_name"] for x in details["names"] if x["include_in_search"]]
    ordered = []
    for x in ([selected_name] if selected_name else []) + [rep] + enabled:
        if x and x not in ordered:
            ordered.append(x)
    include_rule, exclude, notes = [], [], []
    for r in details["rules"]:
        include_rule += [x.strip() for x in (r["include_keywords"] or "").split(",") if x.strip()]
        exclude += [x.strip() for x in (r["exclude_keywords"] or "").split(",") if x.strip()]
        if r["rule_notes"]:
            notes.append(r["rule_notes"])
    for x in include_rule:
        if x not in ordered:
            ordered.append(x)
    # 네이버 데이터랩 keyword group은 최대 20개 검색어를 허용한다.
    return {"item_id": item_id, "group_name": selected_name or rep, "representative_name": rep,
            "keywords": ordered[:20], "exclude_keywords": list(dict.fromkeys(exclude)),
            "rule_notes": notes, "forms": details["forms"],
            "categories": [x["category_name"] for x in details["categories"]]}


def add_item(category: str, representative_name: str, search_name: Optional[str] = None,
             season_type: str = "미정", name_type: str = "대표명") -> int:
    now = datetime.now().isoformat(timespec="seconds")
    representative_name = representative_name.strip()
    search_name = (search_name or representative_name).strip()
    if not representative_name or not search_name:
        raise ValueError("품목명을 입력하세요.")
    with connect() as con:
        row = con.execute("SELECT item_id FROM items WHERE representative_name=?", (representative_name,)).fetchone()
        if row:
            item_id = int(row[0])
        else:
            cur = con.execute(
                "INSERT INTO items(representative_name,season_type_initial,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                (representative_name, season_type, "active", now, now),
            )
            item_id = int(cur.lastrowid)
            cat = con.execute("SELECT category_id FROM categories WHERE category_name=?", (category,)).fetchone()
            if not cat:
                raise ValueError(f"없는 카테고리입니다: {category}")
            con.execute("INSERT OR IGNORE INTO item_categories(item_id,category_id,is_primary) VALUES(?,?,1)", (item_id, int(cat[0])))
        actual_type = "대표명" if search_name == representative_name else name_type
        con.execute("INSERT OR IGNORE INTO item_names(item_id,sub_name,name_type,include_in_search) VALUES(?,?,?,1)",
                    (item_id, search_name, actual_type))
        if search_name != representative_name:
            con.execute("INSERT OR IGNORE INTO item_names(item_id,sub_name,name_type,include_in_search) VALUES(?,?,?,1)",
                        (item_id, representative_name, "대표명"))
    return item_id


def add_alias(item_id: int, alias: str, name_type: str = "별칭") -> None:
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO item_names(item_id,sub_name,name_type,include_in_search) VALUES(?,?,?,1)",
                    (item_id, alias.strip(), name_type))


def add_search_rule(item_id: int, include_keywords: str = "", exclude_keywords: str = "", notes: str = "") -> None:
    with connect() as con:
        con.execute("INSERT INTO search_rules(item_id,include_keywords,exclude_keywords,rule_notes) VALUES(?,?,?,?)",
                    (item_id, include_keywords.strip(), exclude_keywords.strip(), notes.strip()))


def add_product_form(item_id: int, form_name: str) -> None:
    with connect() as con:
        row = con.execute("SELECT product_form_id FROM product_forms WHERE form_name=?", (form_name,)).fetchone()
        if not row:
            raise ValueError(f"없는 상품형태입니다: {form_name}")
        con.execute("INSERT OR IGNORE INTO item_product_forms(item_id,product_form_id) VALUES(?,?)", (item_id, int(row[0])))


def product_forms() -> List[str]:
    with connect() as con:
        return [r[0] for r in con.execute("SELECT form_name FROM product_forms ORDER BY product_form_id").fetchall()]


def add_custom(category: str, base_product: str, items: List[str]) -> int:
    before = len(load_database_df())
    for x in items:
        if x.strip():
            add_item(category, base_product.strip() or x.strip(), x.strip(), "미정", "유통명")
    return max(0, len(load_database_df()) - before)


def delete_custom(category: str, items: List[str]) -> int:
    removed = 0
    with connect() as con:
        for name in {x.strip() for x in items if x.strip()}:
            row = con.execute("SELECT name_id FROM item_names WHERE sub_name=?", (name,)).fetchone()
            if row:
                con.execute("DELETE FROM item_names WHERE name_id=?", (int(row[0]),))
                removed += 1
    return removed


def validate_database() -> pd.DataFrame:
    issues = []
    with connect() as con:
        for r in con.execute("SELECT sub_name,COUNT(DISTINCT item_id) n FROM item_names GROUP BY sub_name HAVING n>1"):
            issues.append({"유형":"별칭 충돌","대상":r[0],"내용":f"{r[1]}개 대표품목에 연결"})
        for r in con.execute("""SELECT i.representative_name FROM items i LEFT JOIN item_names n ON n.item_id=i.item_id
                                GROUP BY i.item_id HAVING SUM(CASE WHEN n.name_type='대표명' THEN 1 ELSE 0 END)=0"""):
            issues.append({"유형":"대표명 누락","대상":r[0],"내용":"item_names에 대표명 없음"})
        for r in con.execute("""SELECT i.representative_name FROM items i LEFT JOIN item_categories ic ON ic.item_id=i.item_id AND ic.is_primary=1
                                WHERE ic.item_id IS NULL"""):
            issues.append({"유형":"주카테고리 누락","대상":r[0],"내용":"primary category 없음"})
    return pd.DataFrame(issues, columns=["유형","대상","내용"])


def save_analysis(result: dict, item_id: int, name_id: Optional[int]) -> None:
    cols = ["item_id","name_id","search_keyword","target_year","analysis_years","season_type_calculated",
            "season_type_confidence","recommended_upload_date","entry_date","expected_peak_date","expected_end_date",
            "seasonality_score","offseason_retention_rate","peak_concentration_rate","year_consistency_rate",
            "recent_30d_change","judgement","last_analyzed_at"]
    values = [item_id, name_id] + [result.get(c) for c in cols[2:]]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in {"item_id","name_id","search_keyword","target_year"})
    with connect() as con:
        con.execute(f"INSERT INTO keyword_analysis_results({','.join(cols)}) VALUES({placeholders}) "
                    f"ON CONFLICT(search_keyword,target_year) DO UPDATE SET item_id=excluded.item_id,name_id=excluded.name_id,{updates}", values)


def load_analysis(keyword: Optional[str] = None, target_year: Optional[int] = None) -> pd.DataFrame:
    where, params = [], []
    if keyword:
        where.append("a.search_keyword=?"); params.append(keyword)
    if target_year:
        where.append("a.target_year=?"); params.append(target_year)
    sql = """SELECT a.*, i.representative_name AS 대표품목, c.category_name AS 카테고리
             FROM keyword_analysis_results a JOIN items i ON i.item_id=a.item_id
             LEFT JOIN item_categories ic ON ic.item_id=i.item_id AND ic.is_primary=1
             LEFT JOIN categories c ON c.category_id=ic.category_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.last_analyzed_at DESC, a.search_keyword"
    with connect() as con:
        return pd.read_sql_query(sql, con, params=params)


def get_name_record(name_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM item_names WHERE name_id=?", (name_id,)).fetchone()
    return dict(row) if row else None


def load_evergreen() -> List[str]:
    df = load_database_df()
    return df[df["초기판매유형"] == "사계절형"]["세부품목"].tolist()


def save_evergreen(items: List[str]) -> None:
    pass
