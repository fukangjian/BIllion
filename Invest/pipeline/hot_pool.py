"""
超短热点池 — 涨停/连板/炸板 + 强板块领涨股，1-5 天超短候选来源

每日盘前构建：涨停/炸板名单（东财 push2ex）+ 板块相对强度 Top N 领涨股（同花顺），
合并去重写入 hot_pool 表，并补抓池内个股近 HOT_HISTORY_DAYS 个交易日的日线，
供 market_scanner 对热点池跑 S1-A 突破扫描（信号以 HOT-S 入库验证）。

设计说明：
- 连板判定直接用东财涨停池自带的「连板数」（lbc >= 2），不用跨日比对——
  盘前运行时东财返回的是上一交易日池子，跨日交集会把整池误判为连板。
- 强板块成分股接口（东财 push2 / 本版 akshare 无同花顺成分接口）不可用，
  强板块个股来源降级为同花顺行业一览的「领涨股」（每板块 1 只）。
- 全部数据源失败时产出空池并告警，不阻塞盘前主流程（优雅降级约定）。

用法:
    python pipeline/hot_pool.py            # 构建今日热点池并补抓日线
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging

import pandas as pd

from config import (
    DEFAULT_INDEX_SYMBOL,
    FETCH_MAX_WORKERS,
    HOT_HISTORY_DAYS,
    HOT_POOL_MAX,
    HOT_SECTOR_TOP_N,
)
from pipeline.database import (
    get_connection,
    init_database,
    load_daily_quotes,
    load_sector_quotes,
    save_daily_quotes,
    save_hot_pool,
    save_limit_pool,
)
from pipeline.indicators import rank_sectors_by_strength
from shared.data_fetcher import fetch_limit_pools, fetch_stock_daily
from shared.utils import get_symbol_by_name, retry_fetch

logger = logging.getLogger(__name__)

_SOURCE_PRIORITY = {"连板": 0, "涨停": 1, "炸板": 2, "领涨": 3}


def _strong_sectors(db_path: Optional[Path] = None) -> list[str]:
    """板块相对强度 Top N（复用 indicators 排名，数据全部来自本地 DB，离线可用）"""
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
            return []
        rank = rank_sectors_by_strength(sector_data, benchmark)
        if rank.empty:
            return []
        return rank["sector_name"].head(HOT_SECTOR_TOP_N).tolist()
    except Exception as e:
        logger.warning("板块强度排名失败（热点池跳过领涨股）: %s", e)
        return []


def _fetch_sector_leaders(sectors: list[str]) -> pd.DataFrame:
    """
    强板块领涨股（同花顺行业一览；失败降级空表）。
    返回列: symbol / name / sector / change_pct（symbol 由名称反查，查不到的股票丢弃）。
    """
    if not sectors:
        return pd.DataFrame()

    try:
        import akshare as ak

        summary = retry_fetch(ak.stock_board_industry_summary_ths)
    except Exception as e:
        logger.warning("同花顺行业一览获取失败（跳过领涨股）: %s", e)
        return pd.DataFrame()
    if summary is None or summary.empty:
        return pd.DataFrame()

    records = []
    for sec in sectors:
        matched = summary[summary["板块"].str.contains(sec[:2], na=False)]
        for _, row in matched.head(1).iterrows():
            leader = str(row.get("领涨股", "")).strip()
            if not leader or leader == "-":
                continue
            symbol = get_symbol_by_name(leader)
            if not symbol:
                logger.debug("领涨股 %s 名称反查代码失败，跳过", leader)
                continue
            chg = pd.to_numeric(row.get("领涨股-涨跌幅", 0), errors="coerce")
            records.append({
                "symbol": symbol,
                "name": leader,
                "sector": str(row.get("板块", sec)),
                "change_pct": float(chg) if pd.notna(chg) else 0.0,
            })
    return pd.DataFrame(records)


def _merge_pool(limit_df: pd.DataFrame, leaders: pd.DataFrame) -> pd.DataFrame:
    """
    合并热点池（纯函数，可离线单测）：
    连板 > 涨停 > 炸板 > 领涨，同股多来源合并标注（如「连板+领涨」），
    按来源优先级 + 涨跌幅排序后截断 HOT_POOL_MAX。
    返回列: symbol / name / source / sector / change_pct / lbc
    """
    pool: dict[str, dict] = {}

    if limit_df is not None and not limit_df.empty:
        for _, row in limit_df.iterrows():
            symbol = str(row["symbol"])
            lbc = int(row.get("lbc", 0) or 0)
            if row.get("pool_type") == "up":
                source = "连板" if lbc >= 2 else "涨停"
            else:
                source = "炸板"
            pool[symbol] = {
                "symbol": symbol,
                "name": str(row.get("name", "")),
                "source": source,
                "sector": str(row.get("sector", "")),
                "change_pct": float(row.get("change_pct", 0) or 0),
                "lbc": lbc,
            }

    if leaders is not None and not leaders.empty:
        for _, row in leaders.iterrows():
            symbol = str(row["symbol"])
            if symbol in pool:
                if "领涨" not in pool[symbol]["source"]:
                    pool[symbol]["source"] += "+领涨"
                if not pool[symbol]["sector"]:
                    pool[symbol]["sector"] = str(row.get("sector", ""))
            else:
                pool[symbol] = {
                    "symbol": symbol,
                    "name": str(row.get("name", "")),
                    "source": "领涨",
                    "sector": str(row.get("sector", "")),
                    "change_pct": float(row.get("change_pct", 0) or 0),
                    "lbc": 0,
                }

    records = sorted(
        pool.values(),
        key=lambda r: (
            _SOURCE_PRIORITY.get(r["source"].split("+")[0], 9),
            -r["change_pct"],
        ),
    )
    return pd.DataFrame(records).head(HOT_POOL_MAX) if records else pd.DataFrame()


def build_hot_pool(
    trade_date: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> pd.DataFrame:
    """
    构建今日热点池：抓涨停/炸板名单（存 limit_pool 表）+ 强板块领涨股，
    合并写 hot_pool 表并返回（含 trade_date 列）。全失败返回空 DataFrame。
    """
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    init_database(db_path)

    # 1) 涨停/炸板名单（东财 push2ex），存 limit_pool 表
    limit_df = fetch_limit_pools(trade_date)
    if not limit_df.empty:
        limit_save = limit_df.copy()
        limit_save["trade_date"] = trade_date
        n = save_limit_pool(limit_save, db_path)
        logger.info("涨停/炸板名单入库 %d 条（%s）", n, trade_date)
    else:
        logger.warning("涨停/炸板名单为空（数据源降级），热点池仅含领涨股")

    # 2) 强板块领涨股（板块排名离线，行业一览联网，失败降级空表）
    leaders = _fetch_sector_leaders(_strong_sectors(db_path))

    # 3) 合并去重、写 hot_pool 表
    pool = _merge_pool(limit_df, leaders)
    if pool.empty:
        return pool

    pool = pool.copy()
    pool["trade_date"] = trade_date
    n = save_hot_pool(pool, db_path)
    logger.info(
        "热点池入库 %d 只（连板 %d / 涨停 %d / 炸板 %d / 领涨 %d）",
        n,
        pool["source"].str.contains("连板").sum(),
        (pool["source"] == "涨停").sum(),
        pool["source"].str.contains("炸板").sum(),
        pool["source"].str.contains("领涨").sum(),
    )
    return pool


def _fetch_and_save_hot_stock(sym: str, start_date: str, db_path: Optional[Path]) -> tuple[str, int, Optional[str]]:
    """并行 worker：补抓单只热点股日线并入库，返回 (代码, 行数, 错误信息)"""
    try:
        df = fetch_stock_daily(sym, start_date=start_date)
        rows = save_daily_quotes(df, db_path)
        return sym, rows, None
    except Exception as e:
        return sym, 0, str(e)


def sync_hot_pool_daily(
    pool: pd.DataFrame,
    db_path: Optional[Path] = None,
    max_workers: int = FETCH_MAX_WORKERS,
) -> int:
    """
    补抓热点池个股近 HOT_HISTORY_DAYS 个交易日的日线（并行，单只失败不阻塞）。
    突破扫描只需 20 日通道 + ATR，90 个交易日足够且控制抓取量。
    """
    if pool is None or pool.empty:
        return 0

    symbols = pool["symbol"].dropna().unique().tolist()
    # 交易日目标 × 1.7 ≈ 自然日冗余（90 交易日 ≈ 150 自然日）
    start_date = (datetime.now() - timedelta(days=int(HOT_HISTORY_DAYS * 1.7))).strftime("%Y%m%d")
    total_rows = 0

    logger.info("开始补抓热点池 %d 只个股日线（起始 %s）...", len(symbols), start_date)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_and_save_hot_stock, sym, start_date, db_path): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            sym, rows, err = future.result()
            if err:
                logger.warning("热点股 %s 日线补抓失败（跳过）: %s", sym, err)
            else:
                total_rows += rows

    logger.info("热点池日线补抓完成: %d 行", total_rows)
    return total_rows


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    pool = build_hot_pool()
    if pool.empty:
        print("热点池为空（数据源降级）")
        return
    sync_hot_pool_daily(pool)
    print(f"\n=== 今日热点池（{len(pool)} 只）===")
    print(pool[["symbol", "name", "source", "sector", "change_pct", "lbc"]].to_string(index=False))


if __name__ == "__main__":
    main()
