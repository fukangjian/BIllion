"""
SQLite 数据库管理 — 创建/初始化表、读写封装
"""
import sqlite3
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import pandas as pd

from config import DB_PATH, DATA_DIR

logger = logging.getLogger(__name__)

# 建表 SQL
SCHEMA_SQL = """
-- 日线行情表
CREATE TABLE IF NOT EXISTS daily_quotes (
    symbol      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    amount      REAL,
    PRIMARY KEY (symbol, trade_date)
);

-- 板块行情表
CREATE TABLE IF NOT EXISTS sector_quotes (
    sector_name TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    amount      REAL,
    change_pct  REAL,
    PRIMARY KEY (sector_name, trade_date)
);

-- ETF 资金流向表
CREATE TABLE IF NOT EXISTS etf_flow (
    etf_code    TEXT NOT NULL,
    etf_name    TEXT,
    trade_date  TEXT NOT NULL,
    close       REAL,
    volume      REAL,
    amount      REAL,
    net_flow    REAL,
    PRIMARY KEY (etf_code, trade_date)
);

-- 涨跌停统计表
CREATE TABLE IF NOT EXISTS limit_stats (
    trade_date      TEXT PRIMARY KEY,
    limit_up_count  INTEGER,
    limit_down_count INTEGER,
    broken_count    INTEGER,
    up_count        INTEGER,
    down_count      INTEGER,
    flat_count      INTEGER,
    total_amount    REAL
);

-- 龙虎榜数据表
CREATE TABLE IF NOT EXISTS dragon_tiger (
    symbol      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    name        TEXT,
    close       REAL,
    change_pct  REAL,
    turnover    REAL,
    reason      TEXT,
    buy_amount  REAL,
    sell_amount REAL,
    net_amount  REAL,
    PRIMARY KEY (symbol, trade_date, reason)
);

-- 市场状态记录表
CREATE TABLE IF NOT EXISTS market_state (
    trade_date      TEXT PRIMARY KEY,
    state           TEXT,
    hs300_close     REAL,
    hs300_ma20w     REAL,
    volume_ratio    REAL,
    breadth_ratio   REAL,
    suggested_pos   TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_daily_symbol ON daily_quotes(symbol);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_quotes(trade_date);
CREATE INDEX IF NOT EXISTS idx_sector_date ON sector_quotes(trade_date);
"""


def ensure_data_dir() -> None:
    """确保数据目录存在"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection(db_path: Optional[Path] = None):
    """获取数据库连接（上下文管理器）"""
    ensure_data_dir()
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database(db_path: Optional[Path] = None) -> None:
    """初始化数据库表结构"""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
    logger.info("数据库初始化完成: %s", db_path or DB_PATH)


def upsert_dataframe(
    df: pd.DataFrame,
    table: str,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[Path] = None,
) -> int:
    """
    将 DataFrame 写入数据库（REPLACE 模式）
    返回写入行数
    """
    if df is None or df.empty:
        return 0

    own_conn = conn is None
    if own_conn:
        ctx = get_connection(db_path)
        conn = ctx.__enter__()

    try:
        df.to_sql(table, conn, if_exists="append", index=False)
        # 去重：SQLite 无原生 UPSERT 时用 REPLACE
        # 已在 PRIMARY KEY 约束下，append 可能冲突，改用逐行 REPLACE
    except Exception as e:
        if "UNIQUE constraint" in str(e) or "PRIMARY KEY" in str(e):
            # 逐行替换
            cols = df.columns.tolist()
            placeholders = ",".join(["?"] * len(cols))
            sql = f"REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
            rows = [tuple(row) for row in df.itertuples(index=False, name=None)]
            conn.executemany(sql, rows)
        else:
            raise
    finally:
        if own_conn:
            ctx.__exit__(None, None, None)

    return len(df)


def save_daily_quotes(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    """保存日线行情"""
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "daily_quotes", df)


def save_sector_quotes(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    """保存板块行情"""
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "sector_quotes", df)


def save_etf_flow(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    """保存 ETF 资金流向"""
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "etf_flow", df)


def save_limit_stats(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    """保存涨跌停统计"""
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "limit_stats", df)


def save_dragon_tiger(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    """保存龙虎榜数据"""
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "dragon_tiger", df)


def save_market_state(row: dict, db_path: Optional[Path] = None) -> None:
    """保存市场状态记录"""
    df = pd.DataFrame([row])
    with get_connection(db_path) as conn:
        _replace_rows(conn, "market_state", df)


def _replace_rows(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """REPLACE INTO 批量写入"""
    cols = df.columns.tolist()
    placeholders = ",".join(["?"] * len(cols))
    sql = f"REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    rows = [tuple(r) for r in df.itertuples(index=False, name=None)]
    conn.executemany(sql, rows)
    return len(rows)


def load_daily_quotes(
    symbol: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> pd.DataFrame:
    """读取日线行情"""
    sql = "SELECT * FROM daily_quotes WHERE 1=1"
    params: list = []
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY symbol, trade_date"

    with get_connection(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def load_sector_quotes(
    sector_name: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> pd.DataFrame:
    """读取板块行情"""
    sql = "SELECT * FROM sector_quotes WHERE 1=1"
    params: list = []
    if sector_name:
        sql += " AND sector_name = ?"
        params.append(sector_name)
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY sector_name, trade_date"

    with get_connection(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def load_all_symbols(db_path: Optional[Path] = None) -> list[str]:
    """获取数据库中所有股票代码"""
    with get_connection(db_path) as conn:
        cur = conn.execute("SELECT DISTINCT symbol FROM daily_quotes ORDER BY symbol")
        return [row[0] for row in cur.fetchall()]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_database()
    print(f"数据库已初始化: {DB_PATH}")
