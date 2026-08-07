"""
趋势扫描动态池 — 强势板块成分股（趋势交易候选来源，V5.0 §4.2 板块共振）

每日盘前流程（run_all 取数阶段调用）：
  板块相对强度 Top N（离线，复用 indicators 排名）
  → 东财行业成分股（联网，板块名同花顺→东财模糊匹配，单板块失败降级跳过）
  → 合并去重、剔除禁买板块前缀（BANNED_BOARD_PREFIXES，默认空=不过滤）
  → 截断 TREND_POOL_MAX 写 trend_pool 表
  → 并行补抓池内个股近 TREND_HISTORY_DAYS 个交易日日线

扫描阶段由 market_scanner 读取最新一期趋势池，并入趋势扫描股票池
（WATCHLIST ∪ trend_pool）。与超短热点池（hot_pool，1-5 天）相互独立。

用法:
    python pipeline/trend_pool.py            # 构建今日趋势池并补抓日线
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from config import (
    BANNED_BOARD_PREFIXES,
    DEFAULT_INDEX_SYMBOL,
    FETCH_MAX_WORKERS,
    TREND_HISTORY_DAYS,
    TREND_POOL_MAX,
    TREND_SCAN_TOP_SECTORS,
)
from pipeline.database import (
    get_connection,
    init_database,
    load_daily_quotes,
    load_sector_quotes,
    save_daily_quotes,
    save_trend_pool,
)
from pipeline.indicators import rank_sectors_by_strength
from shared.data_fetcher import fetch_sector_constituents, fetch_stock_daily
from shared.utils import normalize_symbol

logger = logging.getLogger(__name__)


def strong_sectors_ranked(db_path: Optional[Path] = None) -> pd.DataFrame:
    """板块相对强度完整排名（数据全部来自本地 DB，离线可用）；失败/无数据返回空表"""
    try:
        benchmark = load_daily_quotes(symbol=DEFAULT_INDEX_SYMBOL, db_path=db_path)
        with get_connection(db_path) as conn:
            names = pd.read_sql_query(
                "SELECT DISTINCT sector_name FROM sector_quotes", conn
            )["sector_name"].tolist()
        sector_data = {}
        for name in names:
            df = load_sector_quotes(sector_name=name, db_path=db_path)
            if not df.empty:
                sector_data[name] = df
        if not sector_data or benchmark.empty:
            return pd.DataFrame()
        return rank_sectors_by_strength(sector_data, benchmark)
    except Exception as e:
        logger.warning("板块强度排名失败（趋势池跳过构建）: %s", e)
        return pd.DataFrame()


def _fetch_constituents_for_sectors(sectors: list[str]) -> pd.DataFrame:
    """
    逐板块抓取成分股（东财，名称自动对齐），单板块失败降级跳过。
    返回列: symbol / name / source_sector（保持板块强度顺序）。
    """
    frames = []
    for sec in sectors:
        cons = fetch_sector_constituents(sec)
        if cons.empty:
            logger.warning("板块 %s 成分股为空（降级跳过）", sec)
            continue
        cons = cons.copy()
        cons["source_sector"] = sec
        frames.append(cons)
    if not frames:
        return pd.DataFrame(columns=["symbol", "name", "source_sector"])
    return pd.concat(frames, ignore_index=True)


def _merge_trend_pool(cons_df: pd.DataFrame) -> pd.DataFrame:
    """
    合并趋势池（纯函数，可离线单测）：
    剔除禁买板块前缀（BANNED_BOARD_PREFIXES，默认空=不过滤）→ 按代码去重（多板块归属以「+」连接板块名，
    保留强度最高板块的首次出现位置）→ 截断 TREND_POOL_MAX。
    返回列: symbol / name / source_sector
    """
    if cons_df is None or cons_df.empty:
        return pd.DataFrame(columns=["symbol", "name", "source_sector"])

    pool: dict[str, dict] = {}
    order: list[str] = []
    for _, row in cons_df.iterrows():
        symbol = normalize_symbol(str(row.get("symbol", "")))
        if not symbol or symbol.startswith(BANNED_BOARD_PREFIXES):
            continue
        sector = str(row.get("source_sector", "") or "")
        if symbol in pool:
            if sector and sector not in pool[symbol]["source_sector"]:
                pool[symbol]["source_sector"] += "+" + sector
            continue
        pool[symbol] = {
            "symbol": symbol,
            "name": str(row.get("name", "") or ""),
            "source_sector": sector,
        }
        order.append(symbol)

    records = [pool[s] for s in order[:TREND_POOL_MAX]]
    return pd.DataFrame(records, columns=["symbol", "name", "source_sector"]) if records else pd.DataFrame(
        columns=["symbol", "name", "source_sector"]
    )


def build_trend_pool(
    trade_date: Optional[str] = None,
    db_path: Optional[Path] = None,
    top_n: int = TREND_SCAN_TOP_SECTORS,
) -> pd.DataFrame:
    """
    构建今日趋势池：板块强度 Top N → 成分股 → 合并去重（剔除禁买前缀，默认空）→ 写 trend_pool 表。
    排名无数据或成分股全失败时返回空 DataFrame（调用方降级，不阻塞主流程）。
    """
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    init_database(db_path)

    rank = strong_sectors_ranked(db_path)
    if rank.empty:
        logger.warning("板块强度排名为空，趋势池未构建")
        return pd.DataFrame()

    top_sectors = rank["sector_name"].head(top_n).tolist()
    logger.info("趋势池来源板块（强度 Top %d）: %s", top_n, ", ".join(top_sectors))

    cons = _fetch_constituents_for_sectors(top_sectors)
    pool = _merge_trend_pool(cons)
    if pool.empty:
        logger.warning("趋势池合并后为空（成分股全部失败或被剔除）")
        return pool

    pool = pool.copy()
    pool["trade_date"] = trade_date
    n = save_trend_pool(pool, db_path)
    logger.info("趋势池入库 %d 只（%s）", n, trade_date)
    return pool


def _fetch_and_save_trend_stock(sym: str, start_date: str, db_path: Optional[Path]) -> tuple[str, int, Optional[str]]:
    """并行 worker：补抓单只趋势池个股日线并入库，返回 (代码, 行数, 错误信息)"""
    try:
        df = fetch_stock_daily(sym, start_date=start_date)
        rows = save_daily_quotes(df, db_path)
        return sym, rows, None
    except Exception as e:
        return sym, 0, str(e)


def sync_trend_pool_daily(
    pool: pd.DataFrame,
    db_path: Optional[Path] = None,
    max_workers: int = FETCH_MAX_WORKERS,
) -> int:
    """
    补抓趋势池个股近 TREND_HISTORY_DAYS 个交易日的日线（并行，单只失败不阻塞）。
    趋势扫描需要 55 日通道 + ATR + 20 周均线，120 个交易日（≈200 自然日）足够。
    """
    if pool is None or pool.empty:
        return 0

    symbols = pool["symbol"].dropna().unique().tolist()
    # 交易日目标 × 1.7 ≈ 自然日冗余（120 交易日 ≈ 200 自然日）
    start_date = (datetime.now() - timedelta(days=int(TREND_HISTORY_DAYS * 1.7))).strftime("%Y%m%d")
    total_rows = 0

    logger.info("开始补抓趋势池 %d 只个股日线（起始 %s）...", len(symbols), start_date)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_and_save_trend_stock, sym, start_date, db_path): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            sym, rows, err = future.result()
            if err:
                logger.warning("趋势池个股 %s 日线补抓失败（跳过）: %s", sym, err)
            else:
                total_rows += rows

    logger.info("趋势池日线补抓完成: %d 行", total_rows)
    return total_rows


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    pool = build_trend_pool()
    if pool.empty:
        print("趋势池为空（数据源降级）")
        return
    sync_trend_pool_daily(pool)
    print(f"\n=== 今日趋势池（{len(pool)} 只）===")
    print(pool[["symbol", "name", "source_sector"]].to_string(index=False))


if __name__ == "__main__":
    main()
