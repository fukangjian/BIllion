"""
数据获取模块 — 使用 AkShare 获取行情、板块、涨跌停、ETF、龙虎榜数据
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import pandas as pd

from config import FETCH_MAX_WORKERS, SECTOR_FETCH_LIMIT, WATCHLIST
from shared.utils import retry_fetch

logger = logging.getLogger(__name__)


def _normalize_date(d) -> str:
    """统一日期格式为 YYYY-MM-DD"""
    if d is None:
        return datetime.now().strftime("%Y-%m-%d")
    s = str(d).replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _sina_symbol(symbol: str) -> str:
    """6位代码转新浪格式 (sh600519 / sz000001)"""
    if symbol.startswith(("6", "5", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def fetch_stock_daily(
    symbol: str,
    start_date: str = "20200101",
    end_date: Optional[str] = None,
    adjust: str = "qfq",
) -> pd.DataFrame:
    """获取个股日线行情，优先新浪源，东方财富备用"""
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")

    try:
        raw = retry_fetch(
            ak.stock_zh_a_daily,
            symbol=_sina_symbol(symbol),
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
        if raw is not None and not raw.empty:
            return pd.DataFrame({
                "symbol": symbol,
                "trade_date": raw["date"].apply(_normalize_date),
                "open": raw["open"].astype(float),
                "high": raw["high"].astype(float),
                "low": raw["low"].astype(float),
                "close": raw["close"].astype(float),
                "volume": raw["volume"].astype(float),
                "amount": raw.get("amount", pd.Series([0] * len(raw))).astype(float),
            })
    except Exception as e:
        logger.warning("新浪源获取 %s 失败，尝试东方财富: %s", symbol, e)

    def _fetch_em():
        return ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )

    try:
        raw = retry_fetch(_fetch_em)
    except Exception as e:
        logger.error("东方财富源也失败: %s", e)
        return pd.DataFrame()

    if raw is None or raw.empty:
        logger.warning("股票 %s 无数据", symbol)
        return pd.DataFrame()

    return pd.DataFrame({
        "symbol": symbol,
        "trade_date": raw["日期"].apply(_normalize_date),
        "open": raw["开盘"].astype(float),
        "high": raw["最高"].astype(float),
        "low": raw["最低"].astype(float),
        "close": raw["收盘"].astype(float),
        "volume": raw["成交量"].astype(float),
        "amount": raw["成交额"].astype(float),
    })


def fetch_index_daily(
    symbol: str = "000300",
    start_date: str = "20200101",
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """获取指数日线（如沪深300）"""
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")

    prefix_map = {"000300": "sh000300", "000001": "sh000001", "399001": "sz399001", "399006": "sz399006"}
    sina_symbol = prefix_map.get(symbol, f"sh{symbol}")

    def _fetch_sina():
        raw = ak.stock_zh_index_daily(symbol=sina_symbol)
        if raw is None or raw.empty:
            return raw
        raw = raw.copy()
        raw["date"] = pd.to_datetime(raw["date"])
        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        return raw[(raw["date"] >= start) & (raw["date"] <= end)]

    def _fetch_em():
        return ak.index_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
        )

    raw = None
    try:
        raw = retry_fetch(_fetch_sina)
    except Exception as e:
        logger.warning("新浪指数接口失败，尝试东方财富: %s", e)
        try:
            raw = retry_fetch(_fetch_em)
        except Exception as e2:
            logger.error("东方财富指数接口也失败: %s", e2)
            return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    if "日期" in raw.columns:
        date_col, o, h, l, c, v, a = "日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"
    else:
        date_col, o, h, l, c, v, a = "date", "open", "high", "low", "close", "volume", "amount"

    return pd.DataFrame({
        "symbol": symbol,
        "trade_date": raw[date_col].apply(_normalize_date),
        "open": raw[o].astype(float),
        "high": raw[h].astype(float),
        "low": raw[l].astype(float),
        "close": raw[c].astype(float),
        "volume": raw[v].astype(float) if v in raw.columns else 0.0,
        "amount": raw[a].astype(float) if a in raw.columns else 0.0,
    })


def fetch_sector_list() -> pd.DataFrame:
    """获取行业板块列表 — 优先同花顺，东方财富备用"""
    try:
        raw = retry_fetch(ak.stock_board_industry_name_ths)
        if raw is not None and not raw.empty:
            return raw
    except Exception as e:
        logger.warning("同花顺板块列表失败: %s", e)

    try:
        raw = retry_fetch(ak.stock_board_industry_name_em)
        if raw is not None and not raw.empty:
            return raw
    except Exception as e:
        logger.warning("东方财富板块列表也失败: %s", e)

    return pd.DataFrame()


def fetch_sector_daily(
    sector_name: str,
    start_date: str = "20240101",
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """获取板块指数日线 — 优先同花顺，东方财富备用"""
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")

    try:
        raw = retry_fetch(
            ak.stock_board_industry_index_ths,
            symbol=sector_name,
            start_date=f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}",
            end_date=f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}",
        )
        if raw is not None and not raw.empty:
            date_col = "日期" if "日期" in raw.columns else raw.columns[0]
            open_col = "开盘价" if "开盘价" in raw.columns else ("开盘" if "开盘" in raw.columns else raw.columns[1])
            high_col = "最高价" if "最高价" in raw.columns else ("最高" if "最高" in raw.columns else raw.columns[2])
            low_col = "最低价" if "最低价" in raw.columns else ("最低" if "最低" in raw.columns else raw.columns[3])
            close_col = "收盘价" if "收盘价" in raw.columns else ("收盘" if "收盘" in raw.columns else raw.columns[4])
            vol_col = "成交量" if "成交量" in raw.columns else None
            amt_col = "成交额" if "成交额" in raw.columns else None
            chg_col = "涨跌幅" if "涨跌幅" in raw.columns else None

            return pd.DataFrame({
                "sector_name": sector_name,
                "trade_date": raw[date_col].apply(_normalize_date),
                "open": pd.to_numeric(raw[open_col], errors="coerce").fillna(0),
                "high": pd.to_numeric(raw[high_col], errors="coerce").fillna(0),
                "low": pd.to_numeric(raw[low_col], errors="coerce").fillna(0),
                "close": pd.to_numeric(raw[close_col], errors="coerce").fillna(0),
                "volume": pd.to_numeric(raw[vol_col], errors="coerce").fillna(0) if vol_col else 0.0,
                "amount": pd.to_numeric(raw[amt_col], errors="coerce").fillna(0) if amt_col else 0.0,
                "change_pct": pd.to_numeric(raw[chg_col], errors="coerce").fillna(0) if chg_col else 0.0,
            })
    except Exception as e:
        logger.warning("同花顺板块 %s 失败: %s", sector_name, e)

    try:
        raw = retry_fetch(
            ak.stock_board_industry_hist_em,
            symbol=sector_name,
            start_date=start_date,
            end_date=end_date,
            period="日k",
            adjust="",
        )
        if raw is not None and not raw.empty:
            return pd.DataFrame({
                "sector_name": sector_name,
                "trade_date": raw["日期"].apply(_normalize_date),
                "open": raw["开盘"].astype(float),
                "high": raw["最高"].astype(float),
                "low": raw["最低"].astype(float),
                "close": raw["收盘"].astype(float),
                "volume": raw["成交量"].astype(float),
                "amount": raw["成交额"].astype(float),
                "change_pct": raw.get("涨跌幅", pd.Series([0] * len(raw))).astype(float),
            })
    except Exception as e:
        logger.warning("东方财富板块 %s 也失败: %s", sector_name, e)

    return pd.DataFrame()


def fetch_limit_stats(trade_date: Optional[str] = None) -> pd.DataFrame:
    """获取涨跌停及市场宽度统计"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")
    date_str = trade_date.replace("-", "")

    limit_up_count = limit_down_count = broken_count = 0

    try:
        zt = retry_fetch(ak.stock_zt_pool_em, date=date_str)
        limit_up_count = len(zt) if zt is not None and not zt.empty else 0
    except Exception as e:
        logger.warning("涨停池获取失败: %s", e)

    try:
        dt = retry_fetch(ak.stock_zt_pool_dtgc_em, date=date_str)
        limit_down_count = len(dt) if dt is not None and not dt.empty else 0
    except Exception as e:
        logger.warning("跌停池获取失败: %s", e)

    try:
        zb = retry_fetch(ak.stock_zt_pool_zbgc_em, date=date_str)
        broken_count = len(zb) if zb is not None and not zb.empty else 0
    except Exception:
        broken_count = 0

    up_count = down_count = flat_count = 0
    total_amount = 0.0
    try:
        spot = retry_fetch(ak.stock_zh_a_spot)
        if spot is not None and not spot.empty:
            change_col = None
            for col in ("changepercent", "涨跌幅", "change"):
                if col in spot.columns:
                    change_col = col
                    break
            if change_col is None:
                change_col = spot.columns[-1]
            changes = pd.to_numeric(spot[change_col], errors="coerce")
            up_count = int((changes > 0).sum())
            down_count = int((changes < 0).sum())
            flat_count = int((changes == 0).sum())
            for amt_col in ("amount", "成交额"):
                if amt_col in spot.columns:
                    total_amount = float(pd.to_numeric(spot[amt_col], errors="coerce").sum())
                    break
    except Exception as e:
        logger.warning("全市场行情获取失败(新浪): %s", e)
        try:
            spot = retry_fetch(ak.stock_zh_a_spot_em)
            if spot is not None and not spot.empty:
                changes = pd.to_numeric(spot.get("涨跌幅", spot.iloc[:, -1]), errors="coerce")
                up_count = int((changes > 0).sum())
                down_count = int((changes < 0).sum())
                flat_count = int((changes == 0).sum())
                if "成交额" in spot.columns:
                    total_amount = float(pd.to_numeric(spot["成交额"], errors="coerce").sum())
        except Exception as e2:
            logger.warning("全市场行情获取失败(东方财富): %s", e2)

    return pd.DataFrame([{
        "trade_date": _normalize_date(date_str),
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "broken_count": broken_count,
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "total_amount": total_amount,
    }])


def fetch_etf_flow(trade_date: Optional[str] = None) -> pd.DataFrame:
    """获取 ETF 资金流向"""
    records = []
    try:
        etf_list = retry_fetch(ak.fund_etf_category_sina, symbol="ETF基金")
        if etf_list is None or etf_list.empty:
            return pd.DataFrame()

        for _, row in etf_list.head(20).iterrows():
            raw_code = str(row.get("代码", ""))
            code = raw_code.replace("sh", "").replace("sz", "")
            name = str(row.get("名称", ""))
            if not code:
                continue
            try:
                sina_sym = raw_code if raw_code.startswith(("sh", "sz")) else f"sh{code}"
                hist = retry_fetch(ak.fund_etf_hist_sina, symbol=sina_sym)
                if hist is None or hist.empty:
                    continue
                latest = hist.iloc[-1]
                prev = hist.iloc[-2] if len(hist) > 1 else latest
                close_val = float(latest.get("close", latest.get("收盘", 0)))
                vol_now = float(latest.get("volume", latest.get("成交量", 0)))
                vol_prev = float(prev.get("volume", prev.get("成交量", 0)))
                date_val = latest.get("date", latest.get("日期", trade_date))
                records.append({
                    "etf_code": code,
                    "etf_name": name,
                    "trade_date": _normalize_date(date_val),
                    "close": close_val,
                    "volume": vol_now,
                    "amount": float(latest.get("amount", latest.get("成交额", 0))),
                    "net_flow": (vol_now - vol_prev) * close_val,
                })
                time.sleep(0.3)
            except Exception as e:
                logger.debug("ETF %s 跳过: %s", code, e)
    except Exception as e:
        logger.warning("ETF 数据获取失败: %s", e)

    return pd.DataFrame(records)


def fetch_dragon_tiger(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """获取龙虎榜数据"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")

    def _fetch():
        return ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)

    try:
        raw = retry_fetch(_fetch)
    except Exception as e:
        logger.warning("龙虎榜获取失败: %s", e)
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    records = []
    for _, row in raw.iterrows():
        records.append({
            "symbol": str(row.get("代码", "")).zfill(6),
            "trade_date": _normalize_date(row.get("上榜日", row.get("日期", ""))),
            "name": str(row.get("名称", "")),
            "close": float(row.get("收盘价", 0) or 0),
            "change_pct": float(row.get("涨跌幅", 0) or 0),
            "turnover": float(row.get("换手率", 0) or 0),
            "reason": str(row.get("解读", row.get("上榜原因", ""))),
            "buy_amount": float(row.get("买入额", row.get("龙虎榜买入额", 0)) or 0),
            "sell_amount": float(row.get("卖出额", row.get("龙虎榜卖出额", 0)) or 0),
            "net_amount": float(row.get("净买额", row.get("龙虎榜净买额", 0)) or 0),
        })
    return pd.DataFrame(records)


def fetch_and_save_all(
    symbols: list[str],
    start_date: str = "20230101",
    sector_limit: int = SECTOR_FETCH_LIMIT,
) -> dict:
    """批量获取并保存所有数据，返回各模块写入行数统计"""
    from pipeline.database import (
        init_database,
        save_daily_quotes,
        save_dragon_tiger,
        save_etf_flow,
        save_limit_stats,
        save_sector_quotes,
    )

    init_database()
    stats = {"daily": 0, "sector": 0, "limit": 0, "etf": 0, "lhb": 0}

    for sym in symbols:
        try:
            df = fetch_stock_daily(sym, start_date=start_date)
            stats["daily"] += save_daily_quotes(df)
            logger.info("已保存 %s 日线 %d 条", sym, len(df))
            time.sleep(0.5)
        except Exception as e:
            logger.error("股票 %s 失败: %s", sym, e)

    try:
        idx_df = fetch_index_daily("000300", start_date=start_date)
        stats["daily"] += save_daily_quotes(idx_df)
    except Exception as e:
        logger.error("沪深300 失败: %s", e)

    try:
        sectors = fetch_sector_list()
        for _, srow in sectors.head(sector_limit).iterrows():
            name = srow.get("name", srow.get("板块名称", ""))
            if not name and len(srow) > 1:
                name = str(srow.iloc[0])
            if not name:
                continue
            df = fetch_sector_daily(str(name), start_date=start_date)
            stats["sector"] += save_sector_quotes(df)
            time.sleep(0.5)
    except Exception as e:
        logger.error("板块数据失败: %s", e)

    try:
        limit_df = fetch_limit_stats()
        stats["limit"] += save_limit_stats(limit_df)
    except Exception as e:
        logger.error("涨跌停统计失败: %s", e)

    try:
        etf_df = fetch_etf_flow()
        stats["etf"] += save_etf_flow(etf_df)
    except Exception as e:
        logger.error("ETF 失败: %s", e)

    try:
        lhb_df = fetch_dragon_tiger()
        stats["lhb"] += save_dragon_tiger(lhb_df)
    except Exception as e:
        logger.error("龙虎榜失败: %s", e)

    return stats


def _fetch_and_save_single_stock(sym: str, start_date: str) -> tuple[str, int, Optional[str]]:
    """并行 worker：获取并保存单只股票日线，返回 (代码, 行数, 错误信息)"""
    from pipeline.database import save_daily_quotes

    try:
        df = fetch_stock_daily(sym, start_date=start_date)
        rows = save_daily_quotes(df)
        return sym, rows, None
    except Exception as e:
        return sym, 0, str(e)


def fetch_and_save_all_parallel(
    symbols: list[str],
    start_date: str = "20230101",
    sector_limit: int = SECTOR_FETCH_LIMIT,
    max_workers: int = FETCH_MAX_WORKERS,
) -> dict:
    """并行批量获取并保存数据（日线并行，全局数据串行），返回各模块写入行数统计"""
    from pipeline.database import (
        init_database,
        save_daily_quotes,
        save_dragon_tiger,
        save_etf_flow,
        save_limit_stats,
        save_sector_quotes,
    )

    init_database()
    stats = {"daily": 0, "sector": 0, "limit": 0, "etf": 0, "lhb": 0}

    # 日线数据：ThreadPoolExecutor 并行获取
    logger.info("开始并行获取 %d 只股票日线 (workers=%d)...", len(symbols), max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_and_save_single_stock, sym, start_date): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            sym, rows, err = future.result()
            if err:
                logger.error("股票 %s 失败: %s", sym, err)
            else:
                stats["daily"] += rows
                logger.info("已保存 %s 日线 %d 条", sym, rows)

    # 沪深300 指数（串行）
    try:
        idx_df = fetch_index_daily("000300", start_date=start_date)
        stats["daily"] += save_daily_quotes(idx_df)
        logger.info("已保存沪深300 日线 %d 条", len(idx_df))
    except Exception as e:
        logger.error("沪深300 失败: %s", e)

    # 板块数据（串行，全局数据不宜并行）
    try:
        sectors = fetch_sector_list()
        for _, srow in sectors.head(sector_limit).iterrows():
            name = srow.get("name", srow.get("板块名称", ""))
            if not name and len(srow) > 1:
                name = str(srow.iloc[0])
            if not name:
                continue
            df = fetch_sector_daily(str(name), start_date=start_date)
            stats["sector"] += save_sector_quotes(df)
            time.sleep(0.5)
    except Exception as e:
        logger.error("板块数据失败: %s", e)

    # 涨跌停统计（串行）
    try:
        limit_df = fetch_limit_stats()
        stats["limit"] += save_limit_stats(limit_df)
    except Exception as e:
        logger.error("涨跌停统计失败: %s", e)

    # ETF 资金流向（串行）
    try:
        etf_df = fetch_etf_flow()
        stats["etf"] += save_etf_flow(etf_df)
    except Exception as e:
        logger.error("ETF 失败: %s", e)

    # 龙虎榜（串行）
    try:
        lhb_df = fetch_dragon_tiger()
        stats["lhb"] += save_dragon_tiger(lhb_df)
    except Exception as e:
        logger.error("龙虎榜失败: %s", e)

    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("开始获取数据...")
    result = fetch_and_save_all(WATCHLIST[:3], start_date="20240101")
    print("完成:", result)
