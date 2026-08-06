"""
三重滤网纯函数测试 — mock 日线 DataFrame，不依赖网络（V5.0 §4.2/§4.3）
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.trend_filters import evaluate_trend_filters, filters_brief


def _make_df(closes: list[float], amount_last: float | None = None) -> pd.DataFrame:
    """按收盘价序列构造日线（amount 默认恒为 100，可用 amount_last 覆盖最后一根）"""
    n = len(closes)
    dates = pd.date_range("2025-01-06", periods=n, freq="B").strftime("%Y-%m-%d")
    amounts = [100.0] * n
    if amount_last is not None:
        amounts[-1] = amount_last
    close = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "trade_date": dates,
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 1_000_000.0,
        "amount": amounts,
    })


def _rising(n: int = 150) -> pd.DataFrame:
    return _make_df((100 + np.arange(n) * 0.5).tolist())


class TestWeeklyTrend:
    def test_above_20w_ma(self):
        """上升趋势：收盘价在 20 周均线上方"""
        assert evaluate_trend_filters(_rising())["weekly_trend"] is True

    def test_below_20w_ma(self):
        """下降趋势：收盘价跌破 20 周均线"""
        df = _make_df((200 - np.arange(150) * 0.5).tolist())
        assert evaluate_trend_filters(df)["weekly_trend"] is False

    def test_insufficient_data_is_none(self):
        """不足 20 周数据 → None（无法判定，不计False）"""
        df = _make_df((100 + np.arange(30) * 0.5).tolist())
        assert evaluate_trend_filters(df)["weekly_trend"] is None


class TestSectorStrength:
    def test_top_pct_passes(self):
        """板块排名前 10%（≤20% 阈值）→ 通过"""
        assert evaluate_trend_filters(_rising(), sector_rank_pct=0.10)["sector_strength"] is True

    def test_low_pct_fails(self):
        assert evaluate_trend_filters(_rising(), sector_rank_pct=0.50)["sector_strength"] is False

    def test_unknown_sector_is_none(self):
        """池外个股（无板块排名）→ None 降级"""
        assert evaluate_trend_filters(_rising(), sector_rank_pct=None)["sector_strength"] is None


class TestVolumeConfirm:
    def test_amount_above_median(self):
        """当日成交额 150 ≥ 过去 20 日中位数 100 → 通过"""
        df = _rising()
        df.loc[df.index[-1], "amount"] = 150.0
        assert evaluate_trend_filters(df)["volume_confirm"] is True

    def test_amount_below_median(self):
        df = _rising()
        df.loc[df.index[-1], "amount"] = 50.0
        assert evaluate_trend_filters(df)["volume_confirm"] is False

    def test_insufficient_amount_is_none(self):
        df = _make_df((100 + np.arange(10) * 0.5).tolist())
        assert evaluate_trend_filters(df)["volume_confirm"] is None


class TestMaBullish:
    def test_ma20_above_ma60(self):
        assert evaluate_trend_filters(_rising())["ma_bullish"] is True

    def test_ma20_below_ma60(self):
        df = _make_df((200 - np.arange(100) * 0.5).tolist())
        assert evaluate_trend_filters(df)["ma_bullish"] is False

    def test_short_data_is_none(self):
        df = _make_df((100 + np.arange(40) * 0.5).tolist())
        assert evaluate_trend_filters(df)["ma_bullish"] is None


class TestRequiredCounts:
    def test_s1a_requires_three(self):
        """S1-A 必需 3 项（周线/板块/量能），ma_bullish 不计"""
        r = evaluate_trend_filters(_rising(), sector_rank_pct=0.1, system="S1-A")
        assert r["required"] == 3
        assert r["passed"] == 3
        assert r["all_passed"] is True

    def test_s2a_requires_four(self):
        """S2-A 必需 4 项（加 MA20>MA60）"""
        r = evaluate_trend_filters(_rising(), sector_rank_pct=0.1, system="S2-A")
        assert r["required"] == 4
        assert r["all_passed"] is True

    def test_s2a_ma_bearish_blocks_all_passed(self):
        """S2-A 均线空头 → all_passed False；通过数 = 板块✓ + 量能✓（周线/均线不通过）"""
        df = _make_df((200 - np.arange(100) * 0.5).tolist())
        r2 = evaluate_trend_filters(df, sector_rank_pct=0.1, system="S2-A")
        assert r2["ma_bullish"] is False
        assert r2["all_passed"] is False
        assert r2["passed"] == 2

    def test_none_not_counted_as_passed(self):
        """板块未知（None）→ 不计入通过数，all_passed False"""
        r = evaluate_trend_filters(_rising(), sector_rank_pct=None, system="S1-A")
        assert r["passed"] == 2  # 周线✓ 量能✓（恒量：当日=中位数 → True）
        assert r["all_passed"] is False


class TestFiltersBrief:
    def test_brief_marks(self):
        text = filters_brief({
            "weekly_trend": True, "sector_strength": False,
            "volume_confirm": True, "ma_bullish": None,
        })
        assert "周线✓" in text
        assert "板块✗" in text
        assert "量能✓" in text
        assert "均线-" in text
