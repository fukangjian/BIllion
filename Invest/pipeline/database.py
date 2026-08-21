"""
SQLite 数据库管理 — 创建/初始化表、读写封装
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import sqlite3
import logging
from contextlib import contextmanager
from typing import Optional

import pandas as pd

from config import DB_PATH, DATA_DIR

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
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

CREATE TABLE IF NOT EXISTS signals (
    signal_date     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    system          TEXT NOT NULL,
    entry_price     REAL,
    stop_price      REAL,
    channel_period  INTEGER,
    status          TEXT NOT NULL DEFAULT 'open',
    exit_date       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,
    r_multiple      REAL,
    market_state    TEXT,
    filter_passed   INTEGER,
    note            TEXT,
    created_at      TEXT,
    PRIMARY KEY (signal_date, symbol, system)
);

CREATE TABLE IF NOT EXISTS limit_pool (
    trade_date  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    name        TEXT,
    pool_type   TEXT NOT NULL,
    change_pct  REAL,
    amount      REAL,
    lbc         INTEGER,
    sector      TEXT,
    fbt         TEXT,
    seal_amount REAL,
    turnover    REAL,
    zbc         INTEGER,
    reason      TEXT,
    latest      REAL,
    circular_cap REAL,
    PRIMARY KEY (trade_date, symbol, pool_type)
);

CREATE TABLE IF NOT EXISTS hot_pool (
    trade_date  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    name        TEXT,
    source      TEXT,
    sector      TEXT,
    change_pct  REAL,
    lbc         INTEGER,
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS trend_pool (
    trade_date    TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    name          TEXT,
    source_sector TEXT,
    PRIMARY KEY (trade_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_daily_symbol ON daily_quotes(symbol);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_quotes(trade_date);
CREATE INDEX IF NOT EXISTS idx_sector_date ON sector_quotes(trade_date);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_limit_pool_date ON limit_pool(trade_date);
CREATE INDEX IF NOT EXISTS idx_hot_pool_date ON hot_pool(trade_date);
CREATE INDEX IF NOT EXISTS idx_trend_pool_date ON trend_pool(trade_date);
"""


def _migrate_signals_columns(conn: sqlite3.Connection) -> None:
    """老库 signals 表补列（幂等）：market_state / filter_passed / note（2026-08 信号分层与滤网标注）"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
    for col, ddl in (("market_state", "TEXT"), ("filter_passed", "INTEGER"), ("note", "TEXT")):
        if col not in existing:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {ddl}")


def _migrate_limit_pool_columns(conn: sqlite3.Connection) -> None:
    """老库 limit_pool 表补列（幂等）：fbt/seal_amount/turnover/zbc/reason（2026-08 龙头评分数据）；
    latest/circular_cap（2026-08 二板战法硬过滤数据）"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(limit_pool)")}
    for col, ddl in (("fbt", "TEXT"), ("seal_amount", "REAL"),
                     ("turnover", "REAL"), ("zbc", "INTEGER"), ("reason", "TEXT"),
                     ("latest", "REAL"), ("circular_cap", "REAL")):
        if col not in existing:
            conn.execute(f"ALTER TABLE limit_pool ADD COLUMN {col} {ddl}")


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
    """初始化数据库表结构（含老库列迁移）"""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_signals_columns(conn)
        _migrate_limit_pool_columns(conn)
    logger.info("数据库初始化完成: %s", db_path or DB_PATH)


def upsert_dataframe(
    df: pd.DataFrame,
    table: str,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[Path] = None,
) -> int:
    """将 DataFrame 写入数据库（REPLACE 模式）"""
    if df is None or df.empty:
        return 0

    own_conn = conn is None
    if own_conn:
        ctx = get_connection(db_path)
        conn = ctx.__enter__()

    try:
        df.to_sql(table, conn, if_exists="append", index=False)
    except Exception as e:
        if "UNIQUE constraint" in str(e) or "PRIMARY KEY" in str(e):
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
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "daily_quotes", df)


def save_sector_quotes(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "sector_quotes", df)


def save_etf_flow(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "etf_flow", df)


def save_limit_stats(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "limit_stats", df)


def save_limit_pool(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "limit_pool", df)


def save_hot_pool(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "hot_pool", df)


def save_trend_pool(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "trend_pool", df)


def save_dragon_tiger(df: pd.DataFrame, db_path: Optional[Path] = None) -> int:
    if df.empty:
        return 0
    with get_connection(db_path) as conn:
        return _replace_rows(conn, "dragon_tiger", df)


def save_market_state(row: dict, db_path: Optional[Path] = None) -> None:
    df = pd.DataFrame([row])
    with get_connection(db_path) as conn:
        _replace_rows(conn, "market_state", df)


def _replace_rows(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
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


def load_limit_pool(
    trade_date: Optional[str] = None,
    pool_type: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> pd.DataFrame:
    sql = "SELECT * FROM limit_pool WHERE 1=1"
    params: list = []
    if trade_date:
        sql += " AND trade_date = ?"
        params.append(trade_date)
    if pool_type:
        sql += " AND pool_type = ?"
        params.append(pool_type)
    sql += " ORDER BY trade_date DESC, symbol"

    with get_connection(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def load_hot_pool(
    trade_date: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> pd.DataFrame:
    sql = "SELECT * FROM hot_pool WHERE 1=1"
    params: list = []
    if trade_date:
        sql += " AND trade_date = ?"
        params.append(trade_date)
    sql += " ORDER BY trade_date DESC, lbc DESC, change_pct DESC"

    with get_connection(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def load_trend_pool(
    trade_date: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> pd.DataFrame:
    """读取趋势池；trade_date 为 None 时取最新一期"""
    if trade_date:
        sql = "SELECT * FROM trend_pool WHERE trade_date = ? ORDER BY symbol"
        params: list = [trade_date]
    else:
        sql = ("SELECT * FROM trend_pool "
               "WHERE trade_date = (SELECT MAX(trade_date) FROM trend_pool) ORDER BY symbol")
        params = []

    with get_connection(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def load_all_symbols(db_path: Optional[Path] = None) -> list[str]:
    with get_connection(db_path) as conn:
        cur = conn.execute("SELECT DISTINCT symbol FROM daily_quotes ORDER BY symbol")
        return [row[0] for row in cur.fetchall()]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_database()
    print(f"数据库已初始化: {DB_PATH}")
