"""
技术指标计算 — ATR、通道、板块相对强度、市场宽度、成交额变化
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging

import numpy as np
import pandas as pd

from config import ATR_PERIOD, CHANNEL_LONG, CHANNEL_SHORT

logger = logging.getLogger(__name__)


def calc_true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def calc_atr(
    df: pd.DataFrame,
    period: int = ATR_PERIOD,
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
) -> pd.Series:
    if df.empty or len(df) < 2:
        return pd.Series(dtype=float)

    tr = calc_true_range(df[high_col], df[low_col], df[close_col])
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def calc_donchian_channel(
    df: pd.DataFrame,
    period: int = CHANNEL_SHORT,
    high_col: str = "high",
    low_col: str = "low",
) -> pd.DataFrame:
    result = df.copy()
    result[f"high_{period}"] = df[high_col].rolling(period).max().shift(1)
    result[f"low_{period}"] = df[low_col].rolling(period).min().shift(1)
    return result


def add_all_channels(df: pd.DataFrame) -> pd.DataFrame:
    df = calc_donchian_channel(df, CHANNEL_SHORT)
    df = calc_donchian_channel(df, CHANNEL_LONG)
    df["atr_20"] = calc_atr(df, ATR_PERIOD)
    return df


def detect_breakout(
    df: pd.DataFrame,
    period: int = CHANNEL_SHORT,
    close_col: str = "close",
) -> pd.Series:
    high_col = f"high_{period}"
    if high_col not in df.columns:
        df = calc_donchian_channel(df, period)
    return df[close_col] > df[high_col]


def calc_sector_relative_strength(
    sector_df: pd.DataFrame,
    benchmark_df: pd.DataFrame,
    period: int = 20,
) -> pd.DataFrame:
    if sector_df.empty or benchmark_df.empty:
        return pd.DataFrame()

    sec = sector_df.sort_values("trade_date").copy()
    bench = benchmark_df.sort_values("trade_date").copy()

    sec["sec_ret"] = sec["close"].pct_change(period)
    bench["bench_ret"] = bench["close"].pct_change(period)

    merged = sec.merge(
        bench[["trade_date", "bench_ret"]],
        on="trade_date",
        how="inner",
    )
    merged["relative_strength"] = merged["sec_ret"] / merged["bench_ret"].replace(0, np.nan)
    return merged


def rank_sectors_by_strength(
    sector_data: dict[str, pd.DataFrame],
    benchmark_df: pd.DataFrame,
    period: int = 20,
) -> pd.DataFrame:
    rows = []
    for name, sdf in sector_data.items():
        rs_df = calc_sector_relative_strength(sdf, benchmark_df, period)
        if rs_df.empty:
            continue
        latest = rs_df.iloc[-1]
        rows.append({
            "sector_name": name,
            "trade_date": latest["trade_date"],
            "close": latest["close"],
            "change_pct": latest.get("change_pct", 0),
            "relative_strength": latest["relative_strength"],
            "period_return": latest["sec_ret"],
        })

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(rows)
    result = result.sort_values("relative_strength", ascending=False).reset_index(drop=True)
    result["rank"] = range(1, len(result) + 1)
    return result


def calc_market_breadth(limit_stats: pd.DataFrame) -> dict:
    if limit_stats.empty:
        return {"breadth_ratio": 1.0, "limit_up_ratio": 0.0, "up_count": 0, "down_count": 0}

    # 调用方返回的行序不固定（如 SQL DESC），统一按日期排序后取最新一天
    if "trade_date" in limit_stats.columns:
        limit_stats = limit_stats.sort_values("trade_date")
    row = limit_stats.iloc[-1]
    up = max(int(row.get("up_count", 0)), 1)
    down = max(int(row.get("down_count", 0)), 1)
    total = up + down + int(row.get("flat_count", 0))

    return {
        "breadth_ratio": up / down,
        "limit_up_ratio": int(row.get("limit_up_count", 0)) / max(total, 1),
        "limit_down_ratio": int(row.get("limit_down_count", 0)) / max(total, 1),
        "up_count": int(row.get("up_count", 0)),
        "down_count": int(row.get("down_count", 0)),
        "total_amount": float(row.get("total_amount", 0)),
    }


def calc_volume_change(amount_series: pd.Series, median_days: int = 60) -> dict:
    if amount_series.empty:
        return {"current": 0, "median": 0, "ratio": 1.0}

    recent = amount_series.dropna()
    if len(recent) < 2:
        return {"current": 0, "median": 0, "ratio": 1.0}

    current = float(recent.iloc[-1])
    window = recent.tail(median_days)
    median = float(window.median())
    ratio = current / median if median > 0 else 1.0

    return {"current": current, "median": median, "ratio": ratio}


def calc_weekly_ma(
    df: pd.DataFrame,
    weeks: int = 20,
    close_col: str = "close",
    date_col: str = "trade_date",
) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)

    tmp = df.copy()
    tmp[date_col] = pd.to_datetime(tmp[date_col])
    tmp = tmp.set_index(date_col).sort_index()
    weekly = tmp[close_col].resample("W-FRI").last()
    ma = weekly.rolling(weeks).mean()
    daily_ma = ma.reindex(tmp.index, method="ffill")
    return daily_ma.reset_index(drop=True)


def prepare_stock_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.sort_values("trade_date").reset_index(drop=True)
    return add_all_channels(df)


def scan_breakout_candidates(
    symbols_data: dict[str, pd.DataFrame],
    period: int = CHANNEL_SHORT,
) -> pd.DataFrame:
    rows = []
    for symbol, df in symbols_data.items():
        if df.empty or len(df) < period + 5:
            continue
        df = prepare_stock_indicators(df)
        latest = df.iloc[-1]
        high_col = f"high_{period}"
        if pd.isna(latest.get(high_col)):
            continue

        if latest["close"] > latest[high_col]:
            rows.append({
                "symbol": symbol,
                "trade_date": latest["trade_date"],
                "close": latest["close"],
                "channel_high": latest[high_col],
                "atr_20": latest.get("atr_20", np.nan),
                "breakout_pct": (latest["close"] / latest[high_col] - 1) * 100,
                "period": period,
            })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("breakout_pct", ascending=False)


def judge_market_state(
    hs300_df: pd.DataFrame,
    limit_stats: pd.DataFrame,
    ma_weeks: int = 20,
    volume_median_days: int = 60,
    breadth_bull: float = 1.5,
    breadth_bear: float = 0.7,
) -> dict:
    result = {
        "state": "C",
        "suggested_pos": "20%-50%",
        "hs300_close": None,
        "hs300_ma20w": None,
        "volume_ratio": 1.0,
        "breadth_ratio": 1.0,
        "notes": [],
    }

    if hs300_df.empty:
        result["notes"].append("缺少沪深300数据，默认震荡(C)")
        return result

    hs300 = hs300_df.sort_values("trade_date").reset_index(drop=True)
    ma20w = calc_weekly_ma(hs300, ma_weeks)
    latest_close = float(hs300.iloc[-1]["close"])
    latest_ma = float(ma20w.iloc[-1]) if not ma20w.empty and not pd.isna(ma20w.iloc[-1]) else latest_close

    result["hs300_close"] = latest_close
    result["hs300_ma20w"] = latest_ma
    above_ma = latest_close >= latest_ma

    vol_info = calc_volume_change(hs300["amount"], volume_median_days)
    result["volume_ratio"] = vol_info["ratio"]
    high_volume = vol_info["ratio"] >= 1.0

    breadth = calc_market_breadth(limit_stats)
    result["breadth_ratio"] = breadth["breadth_ratio"]
    strong_breadth = breadth["breadth_ratio"] >= breadth_bull
    weak_breadth = breadth["breadth_ratio"] <= breadth_bear

    if above_ma and high_volume and strong_breadth:
        result["state"] = "A"
        result["suggested_pos"] = "70%-90%"
        result["notes"].append("指数在20周均线上方，成交活跃，涨跌比偏多")
    elif above_ma and (high_volume or strong_breadth):
        result["state"] = "B"
        result["suggested_pos"] = "40%-70%"
        result["notes"].append("指数偏强但共振不足，结构性行情")
    elif not above_ma and weak_breadth:
        result["state"] = "D"
        result["suggested_pos"] = "0%-30%"
        result["notes"].append("指数跌破20周均线，涨跌比偏弱，系统性下跌")
    else:
        result["state"] = "C"
        result["suggested_pos"] = "20%-50%"
        result["notes"].append("指数与宽度信号不一致，震荡轮动")

    return result
