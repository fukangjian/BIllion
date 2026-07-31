"""
技术指标纯函数测试 — 使用 mock DataFrame，不依赖网络
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.indicators import (
    calc_atr,
    calc_donchian_channel,
    calc_true_range,
    detect_breakout,
    scan_breakout_candidates,
)


def _make_ohlc(n: int = 30, base: float = 100.0) -> pd.DataFrame:
    """构造模拟 OHLC 数据"""
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    close = pd.Series(base + np.arange(n) * 0.5, index=dates)
    return pd.DataFrame({
        "trade_date": dates.strftime("%Y-%m-%d"),
        "open": close - 0.3,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 1_000_000,
        "amount": 100_000_000.0,
    })


class TestCalcTrueRange:
    def test_basic_true_range(self):
        df = _make_ohlc(5)
        tr = calc_true_range(df["high"], df["low"], df["close"])
        assert len(tr) == 5
        assert tr.iloc[0] == pytest.approx(2.0)  # high - low
        assert tr.iloc[1] > 0


class TestCalcATR:
    def test_atr_returns_series(self):
        df = _make_ohlc(25)
        atr = calc_atr(df, period=20)
        assert len(atr) == 25
        assert not atr.iloc[-1:].isna().all()

    def test_atr_empty_dataframe(self):
        atr = calc_atr(pd.DataFrame(), period=20)
        assert atr.empty

    def test_atr_insufficient_data(self):
        df = _make_ohlc(2)
        atr = calc_atr(df, period=20)
        assert len(atr) == 2


class TestDonchianChannel:
    def test_channel_columns_added(self):
        df = _make_ohlc(25)
        result = calc_donchian_channel(df, period=20)
        assert "high_20" in result.columns
        assert "low_20" in result.columns

    def test_channel_high_is_shifted(self):
        df = _make_ohlc(25)
        result = calc_donchian_channel(df, period=5)
        # 第 6 行的 high_5 应基于前 5 日最高（不含当日）
        assert pd.notna(result.iloc[5]["high_5"])


class TestDetectBreakout:
    def test_detect_breakout_true(self):
        df = _make_ohlc(25)
        df = calc_donchian_channel(df, period=20)
        # 人为制造突破：最后一根收盘价高于通道高点
        df.loc[df.index[-1], "close"] = df.iloc[-1]["high_20"] + 5
        signal = detect_breakout(df, period=20)
        assert signal.iloc[-1] is True or signal.iloc[-1] == True  # noqa: E712

    def test_detect_breakout_false(self):
        df = _make_ohlc(25)
        df = calc_donchian_channel(df, period=20)
        df.loc[df.index[-1], "close"] = df.iloc[-1]["high_20"] - 1
        signal = detect_breakout(df, period=20)
        assert not signal.iloc[-1]


class TestScanBreakoutCandidates:
    def test_scan_finds_breakout(self):
        df = _make_ohlc(30)
        df = calc_donchian_channel(df, period=20)
        df.loc[df.index[-1], "close"] = df.iloc[-1]["high_20"] + 10
        df.loc[df.index[-1], "high"] = df.iloc[-1]["close"] + 1

        from pipeline.indicators import add_all_channels
        df = add_all_channels(df.sort_values("trade_date").reset_index(drop=True))

        symbols_data = {"600519": df}
        result = scan_breakout_candidates(symbols_data, period=20)
        assert not result.empty
        assert result.iloc[0]["symbol"] == "600519"

    def test_scan_empty_when_no_breakout(self):
        df = _make_ohlc(30)
        symbols_data = {"600519": df}
        result = scan_breakout_candidates(symbols_data, period=20)
        assert result.empty


class TestCalcMarketBreadth:
    """回归测试：calc_market_breadth 必须使用最新一天的宽度数据。

    历史 bug：market_scanner 的 SQL 按 trade_date DESC 取 5 天（新→旧），
    而 calc_market_breadth 取 iloc[-1]，实际用了 5 天中最旧的一天。
    """

    @staticmethod
    def _make_limit_stats(order: str = "desc") -> pd.DataFrame:
        """构造 5 天涨跌停统计，每天数值不同（最新一天 up_count=4000/down_count=500）"""
        rows = [
            {"trade_date": "2026-07-24", "up_count": 1000, "down_count": 3000,
             "flat_count": 100, "limit_up_count": 10, "limit_down_count": 90,
             "total_amount": 8e11},
            {"trade_date": "2026-07-25", "up_count": 1500, "down_count": 2500,
             "flat_count": 100, "limit_up_count": 20, "limit_down_count": 80,
             "total_amount": 8.5e11},
            {"trade_date": "2026-07-26", "up_count": 2000, "down_count": 2000,
             "flat_count": 100, "limit_up_count": 30, "limit_down_count": 70,
             "total_amount": 9e11},
            {"trade_date": "2026-07-27", "up_count": 3000, "down_count": 1000,
             "flat_count": 100, "limit_up_count": 40, "limit_down_count": 60,
             "total_amount": 9.5e11},
            {"trade_date": "2026-07-28", "up_count": 4000, "down_count": 500,
             "flat_count": 100, "limit_up_count": 50, "limit_down_count": 5,
             "total_amount": 1e12},
        ]
        if order == "desc":
            rows = rows[::-1]  # 模拟 SQL ORDER BY trade_date DESC（新→旧）
        return pd.DataFrame(rows)

    def test_uses_latest_day_with_desc_input(self):
        from pipeline.indicators import calc_market_breadth

        df = self._make_limit_stats(order="desc")
        result = calc_market_breadth(df)
        assert result["up_count"] == 4000
        assert result["down_count"] == 500
        assert result["breadth_ratio"] == pytest.approx(8.0)
        assert result["limit_up_ratio"] == pytest.approx(50 / 4600)

    def test_uses_latest_day_with_asc_input(self):
        from pipeline.indicators import calc_market_breadth

        df = self._make_limit_stats(order="asc")
        result = calc_market_breadth(df)
        assert result["up_count"] == 4000
        assert result["down_count"] == 500
        assert result["breadth_ratio"] == pytest.approx(8.0)

    def test_empty_input_returns_default(self):
        from pipeline.indicators import calc_market_breadth

        result = calc_market_breadth(pd.DataFrame())
        assert result["breadth_ratio"] == 1.0
