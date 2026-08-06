"""
三重滤网量化（V5.0 §4.2/§4.3 可量化部分）— 纯函数，基于本地日线，无未来函数

第一滤网（周线方向）：收盘价 > 20 周均线；
第二滤网（板块共振）：所属板块相对强度排名进入全市场前 TREND_SECTOR_TOP_PCT；
量能确认（S1-A 入场条件②）：当日成交额 ≥ 过去 N 日成交额中位数（不含当日）；
均线排列（S2-A 入场条件②）：MA20 > MA60，仅 S2-A 信号纳入必需项。

「只有价格突破而滤网不足，只能观察或极小仓测试」（V5.0 §4.1）——
滤网结果随信号入库并在扫描报告展示，由人决定仓位级别，工具不自动拦截。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from config import TREND_SECTOR_TOP_PCT, TREND_VOLUME_MEDIAN_DAYS
from pipeline.indicators import calc_weekly_ma

_FILTER_LABELS = {
    "weekly_trend": "周线",
    "sector_strength": "板块",
    "volume_confirm": "量能",
    "ma_bullish": "均线",
}


def evaluate_trend_filters(
    df: pd.DataFrame,
    sector_rank_pct: float | None = None,
    system: str = "S1-A",
) -> dict:
    """
    对单只股票最新一根日线评估三重滤网。

    参数:
        df: 日线数据（含 trade_date/close/volume/amount 列），函数内部按日期排序
        sector_rank_pct: 所属板块强度排名百分位（rank/总数，0.0~1.0）；None 表示无法判定
        system: 信号系统（S2 系列额外要求 ma_bullish）

    返回:
        {weekly_trend, sector_strength, volume_confirm, ma_bullish,
         passed, required, all_passed}
        各项取 True/False/None（数据不足无法判定）；passed/required 只统计必需项
        （S1-A: 周线/板块/量能 3 项；S2-A: 加均线共 4 项），None 不计入 passed。
    """
    result = {
        "weekly_trend": None,
        "sector_strength": None,
        "volume_confirm": None,
        "ma_bullish": None,
    }
    if df is None or df.empty or "close" not in df.columns:
        result.update({"passed": 0, "required": 0, "all_passed": False})
        return result

    df = df.sort_values("trade_date").reset_index(drop=True)
    close = pd.to_numeric(df["close"], errors="coerce")
    latest_close = float(close.iloc[-1])

    # 第一滤网：收盘价 > 20 周均线
    try:
        wma = calc_weekly_ma(df, 20)
        if not wma.empty and pd.notna(wma.iloc[-1]):
            result["weekly_trend"] = bool(latest_close > float(wma.iloc[-1]))
    except Exception:
        pass  # 数据不足保持 None（无法判定）

    # 板块共振：所属板块强度排名前 N%（排名由调用方从板块排名表计算传入）
    if sector_rank_pct is not None:
        result["sector_strength"] = bool(sector_rank_pct <= TREND_SECTOR_TOP_PCT)

    # 量能确认：当日成交额 ≥ 过去 N 日成交额中位数（不含当日）
    if "amount" in df.columns:
        amount = pd.to_numeric(df["amount"], errors="coerce").dropna()
        if len(amount) >= TREND_VOLUME_MEDIAN_DAYS + 1:
            base = amount.iloc[-TREND_VOLUME_MEDIAN_DAYS - 1:-1].median()
            if base > 0:
                result["volume_confirm"] = bool(float(amount.iloc[-1]) >= float(base))

    # 均线排列：MA20 > MA60（S2-A 必需）
    if len(df) >= 61:
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1]
        if pd.notna(ma20) and pd.notna(ma60):
            result["ma_bullish"] = bool(ma20 > ma60)

    required_keys = ["weekly_trend", "sector_strength", "volume_confirm"]
    if "S2" in (system or "").upper():
        required_keys.append("ma_bullish")

    passed = sum(1 for k in required_keys if result[k] is True)
    result.update({
        "passed": passed,
        "required": len(required_keys),
        "all_passed": passed == len(required_keys),
    })
    return result


def filters_brief(filters: dict) -> str:
    """滤网结果紧凑文本（MD 表格用）：周线✓ 板块✗ 量能✓ 均线-（None 显示为 -）"""
    marks = []
    for key, label in _FILTER_LABELS.items():
        v = filters.get(key)
        mark = "✓" if v is True else ("✗" if v is False else "-")
        marks.append(f"{label}{mark}")
    return " ".join(marks)
