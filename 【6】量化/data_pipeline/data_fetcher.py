"""
数据获取模块 — 使用 AkShare 获取行情、板块、涨跌停、ETF、龙虎榜数据
"""
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import pandas as pd


_PROXY_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
_PROXY_PATCHED = False


def _patch_bypass_proxy():
    """
    绕过系统代理直连国内数据源（东方财富/新浪等）。
    Windows 注册表代理 + V2Ray/Clash 等工具会被 requests 自动读取。
    通过 monkey-patch requests.Session 强制禁用代理。
    """
    global _PROXY_PATCHED
    if _PROXY_PATCHED:
        return

    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    for key in _PROXY_KEYS:
        os.environ.pop(key, None)

    import requests
    _original_init = requests.Session.__init__

    def _patched_init(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        self.trust_env = False
        self.proxies = {"http": "", "https": ""}

    requests.Session.__init__ = _patched_init

    _PROXY_PATCHED = True
    logging.getLogger(__name__).info("已绕过系统代理，直连国内数据源")


@contextmanager
def _bypass_proxy():
    """上下文管理器版本（兼容旧调用方式）"""
    _patch_bypass_proxy()
    yield


# 模块加载时立即绕过代理
_patch_bypass_proxy()

from config import FETCH_RETRY, FETCH_RETRY_DELAY, SECTOR_FETCH_LIMIT
from database import (
    init_database,
    save_daily_quotes,
    save_dragon_tiger,
    save_etf_flow,
    save_limit_stats,
    save_sector_quotes,
)

logger = logging.getLogger(__name__)


def _retry_fetch(func, *args, **kwargs):
    """带重试的数据获取，自动绕过代理直连国内数据源"""
    last_err = None
    with _bypass_proxy():
        for attempt in range(FETCH_RETRY):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_err = e
                logger.warning("获取失败 (尝试 %d/%d): %s", attempt + 1, FETCH_RETRY, e)
                if attempt < FETCH_RETRY - 1:
                    time.sleep(FETCH_RETRY_DELAY * (attempt + 1))
    raise last_err


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
    s = symbol.lstrip("0") if symbol.startswith("0") else symbol
    if symbol.startswith(("6", "5", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def fetch_stock_daily(
    symbol: str,
    start_date: str = "20200101",
    end_date: Optional[str] = None,
    adjust: str = "qfq",
) -> pd.DataFrame:
    """
    获取个股日线行情
    优先使用新浪源（不限制境外 IP），东方财富作为备用
    """
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")

    # 优先新浪源
    try:
        raw = _retry_fetch(
            ak.stock_zh_a_daily,
            symbol=_sina_symbol(symbol),
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
        if raw is not None and not raw.empty:
            df = pd.DataFrame({
                "symbol": symbol,
                "trade_date": raw["date"].apply(_normalize_date),
                "open": raw["open"].astype(float),
                "high": raw["high"].astype(float),
                "low": raw["low"].astype(float),
                "close": raw["close"].astype(float),
                "volume": raw["volume"].astype(float),
                "amount": raw.get("amount", pd.Series([0] * len(raw))).astype(float),
            })
            return df
    except Exception as e:
        logger.warning("新浪源获取 %s 失败，尝试东方财富: %s", symbol, e)

    # 备用东方财富源
    def _fetch_em():
        return ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )

    try:
        raw = _retry_fetch(_fetch_em)
    except Exception as e:
        logger.error("东方财富源也失败: %s", e)
        return pd.DataFrame()

    if raw is None or raw.empty:
        logger.warning("股票 %s 无数据", symbol)
        return pd.DataFrame()

    df = pd.DataFrame({
        "symbol": symbol,
        "trade_date": raw["日期"].apply(_normalize_date),
        "open": raw["开盘"].astype(float),
        "high": raw["最高"].astype(float),
        "low": raw["最低"].astype(float),
        "close": raw["收盘"].astype(float),
        "volume": raw["成交量"].astype(float),
        "amount": raw["成交额"].astype(float),
    })
    return df


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

    # 优先新浪（不限制境外 IP），东方财富备用
    raw = None
    try:
        raw = _retry_fetch(_fetch_sina)
    except Exception as e:
        logger.warning("新浪指数接口失败，尝试东方财富: %s", e)
        try:
            raw = _retry_fetch(_fetch_em)
        except Exception as e2:
            logger.error("东方财富指数接口也失败: %s", e2)
            return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    # 统一列名（兼容 EM 和 Sina 两种格式）
    if "日期" in raw.columns:
        date_col, o, h, l, c, v, a = "日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"
    else:
        date_col, o, h, l, c, v, a = "date", "open", "high", "low", "close", "volume", "amount"

    df = pd.DataFrame({
        "symbol": symbol,
        "trade_date": raw[date_col].apply(_normalize_date),
        "open": raw[o].astype(float),
        "high": raw[h].astype(float),
        "low": raw[l].astype(float),
        "close": raw[c].astype(float),
        "volume": raw[v].astype(float) if v in raw.columns else 0.0,
        "amount": raw[a].astype(float) if a in raw.columns else 0.0,
    })
    return df


def fetch_sector_list() -> pd.DataFrame:
    """获取行业板块列表 — 优先同花顺（不走 push2），东方财富备用"""
    # 同花顺源
    try:
        raw = _retry_fetch(ak.stock_board_industry_name_ths)
        if raw is not None and not raw.empty:
            return raw
    except Exception as e:
        logger.warning("同花顺板块列表失败: %s", e)

    # 东方财富源（datacenter-web，已可直连）
    try:
        raw = _retry_fetch(ak.stock_board_industry_name_em)
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

    # 同花顺源
    try:
        raw = _retry_fetch(
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

            df = pd.DataFrame({
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
            return df
    except Exception as e:
        logger.warning("同花顺板块 %s 失败: %s", sector_name, e)

    # 东方财富备用
    try:
        raw = _retry_fetch(
            ak.stock_board_industry_hist_em,
            symbol=sector_name,
            start_date=start_date,
            end_date=end_date,
            period="日k",
            adjust="",
        )
        if raw is not None and not raw.empty:
            df = pd.DataFrame({
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
            return df
    except Exception as e:
        logger.warning("东方财富板块 %s 也失败: %s", sector_name, e)

    return pd.DataFrame()


def fetch_limit_stats(trade_date: Optional[str] = None) -> pd.DataFrame:
    """
    获取涨跌停及市场宽度统计
    trade_date: YYYY-MM-DD 或 YYYYMMDD
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")
    date_str = trade_date.replace("-", "")

    limit_up_count = 0
    limit_down_count = 0
    broken_count = 0

    # 涨停池
    try:
        zt = _retry_fetch(ak.stock_zt_pool_em, date=date_str)
        limit_up_count = len(zt) if zt is not None and not zt.empty else 0
    except Exception as e:
        logger.warning("涨停池获取失败: %s", e)

    # 跌停池
    try:
        dt = _retry_fetch(ak.stock_zt_pool_dtgc_em, date=date_str)
        limit_down_count = len(dt) if dt is not None and not dt.empty else 0
    except Exception as e:
        logger.warning("跌停池获取失败: %s", e)

    # 炸板池
    try:
        zb = _retry_fetch(ak.stock_zt_pool_zbgc_em, date=date_str)
        broken_count = len(zb) if zb is not None and not zb.empty else 0
    except Exception:
        broken_count = 0

    # 全市场涨跌家数 — 优先新浪（避免 push2.eastmoney.com 被 TUN 拦截）
    up_count = down_count = flat_count = 0
    total_amount = 0.0
    try:
        spot = _retry_fetch(ak.stock_zh_a_spot)
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
            spot = _retry_fetch(ak.stock_zh_a_spot_em)
            if spot is not None and not spot.empty:
                changes = pd.to_numeric(spot.get("涨跌幅", spot.iloc[:, -1]), errors="coerce")
                up_count = int((changes > 0).sum())
                down_count = int((changes < 0).sum())
                flat_count = int((changes == 0).sum())
                if "成交额" in spot.columns:
                    total_amount = float(pd.to_numeric(spot["成交额"], errors="coerce").sum())
        except Exception as e2:
            logger.warning("全市场行情获取失败(东方财富): %s", e2)

    df = pd.DataFrame([{
        "trade_date": _normalize_date(date_str),
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "broken_count": broken_count,
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "total_amount": total_amount,
    }])
    return df


def fetch_etf_flow(trade_date: Optional[str] = None) -> pd.DataFrame:
    """
    获取 ETF 资金流向（新浪 ETF 列表 + 新浪日线估算，不依赖 push2 接口）
    """
    records = []
    try:
        etf_list = _retry_fetch(ak.fund_etf_category_sina, symbol="ETF基金")
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
                hist = _retry_fetch(
                    ak.fund_etf_hist_sina,
                    symbol=sina_sym,
                )
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
        raw = _retry_fetch(_fetch)
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
    """
    批量获取并保存所有数据
    返回各模块写入行数统计
    """
    init_database()
    stats = {"daily": 0, "sector": 0, "limit": 0, "etf": 0, "lhb": 0}

    # 个股日线
    for sym in symbols:
        try:
            df = fetch_stock_daily(sym, start_date=start_date)
            stats["daily"] += save_daily_quotes(df)
            logger.info("已保存 %s 日线 %d 条", sym, len(df))
            time.sleep(0.5)
        except Exception as e:
            logger.error("股票 %s 失败: %s", sym, e)

    # 沪深300
    try:
        idx_df = fetch_index_daily("000300", start_date=start_date)
        stats["daily"] += save_daily_quotes(idx_df)
    except Exception as e:
        logger.error("沪深300 失败: %s", e)

    # 板块（兼容同花顺 name 列和东方财富 板块名称 列）
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

    # 涨跌停
    try:
        limit_df = fetch_limit_stats()
        stats["limit"] += save_limit_stats(limit_df)
    except Exception as e:
        logger.error("涨跌停统计失败: %s", e)

    # ETF
    try:
        etf_df = fetch_etf_flow()
        stats["etf"] += save_etf_flow(etf_df)
    except Exception as e:
        logger.error("ETF 失败: %s", e)

    # 龙虎榜
    try:
        lhb_df = fetch_dragon_tiger()
        stats["lhb"] += save_dragon_tiger(lhb_df)
    except Exception as e:
        logger.error("龙虎榜失败: %s", e)

    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from config import WATCHLIST

    print("开始获取数据...")
    result = fetch_and_save_all(WATCHLIST[:3], start_date="20240101")
    print("完成:", result)
