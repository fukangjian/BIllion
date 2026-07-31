"""
回测策略测试 — 加仓间距上限（0.5N~1N）与按单位真实风险的 R 倍数；
合成行情离线运行 cerebro，不连 AkShare、不读真实 market.db
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import ADD_SPACING_MAX, ADD_SPACING_MIN
from backtest.strategies import (
    S1A_Strategy,
    calc_trade_r_multiple,
    next_add_action,
)


# ---------- 加仓间距上限（海龟法则：每涨 0.5N 加一次，跳空超 1N 不追、跳过不补） ----------

class TestNextAddAction:
    N = 2.0       # ATR
    LAST = 100.0  # 上次加仓价

    def test_add_within_spacing(self):
        """间距 0.6N ∈ [0.5N, 1N] → 加仓，基准更新为当前价"""
        should_add, baseline = next_add_action(self.LAST + 0.6 * self.N, self.LAST, self.N)
        assert should_add is True
        assert baseline == pytest.approx(self.LAST + 0.6 * self.N)

    def test_add_at_exact_bounds(self):
        """间距恰为 0.5N / 1N（含边界）→ 均加仓"""
        for mult in (ADD_SPACING_MIN, ADD_SPACING_MAX):
            should_add, _ = next_add_action(self.LAST + mult * self.N, self.LAST, self.N)
            assert should_add is True

    def test_no_add_below_spacing(self):
        """间距 0.4N < 0.5N → 不加仓，基准不变"""
        should_add, baseline = next_add_action(self.LAST + 0.4 * self.N, self.LAST, self.N)
        assert should_add is False
        assert baseline == self.LAST

    def test_gap_over_max_not_chased(self):
        """跳空 1.3N > 1N → 不追，基准推进 2 档（+1.0N），跳过的单位不补"""
        should_add, baseline = next_add_action(self.LAST + 1.3 * self.N, self.LAST, self.N)
        assert should_add is False
        assert baseline == pytest.approx(self.LAST + 1.0 * self.N)

    def test_gap_then_next_step_adds(self):
        """跳空推进基准后，再涨 0.5N 触发下一档加仓"""
        _, baseline = next_add_action(self.LAST + 1.3 * self.N, self.LAST, self.N)
        should_add, new_baseline = next_add_action(baseline + 0.5 * self.N, baseline, self.N)
        assert should_add is True
        assert new_baseline == pytest.approx(baseline + 0.5 * self.N)

    def test_no_chase_even_with_room_to_add(self):
        """跳空 2.6N：推进 5 档（+2.5N），仍不追（剩余 0.1N 不足 0.5N）"""
        should_add, baseline = next_add_action(self.LAST + 2.6 * self.N, self.LAST, self.N)
        assert should_add is False
        assert baseline == pytest.approx(self.LAST + 2.5 * self.N)


# ---------- R 倍数：按单位真实初始风险分别计算再汇总 ----------

class TestCalcTradeRMultiple:
    def test_single_unit(self):
        """入场 100、单位初始风险 4（止损 96）、卖出 108 → R = (108−100)/4 = 2"""
        r = calc_trade_r_multiple([{"price": 100.0, "shares": 100, "risk": 4.0}], 108.0)
        assert r == pytest.approx(2.0)

    def test_multi_unit_aggregated_per_unit(self):
        """两单位分别算 R 再汇总：(108−100)/4 + (108−102)/4 = 2.0 + 1.5 = 3.5"""
        units = [
            {"price": 100.0, "shares": 100, "risk": 4.0},
            {"price": 102.0, "shares": 100, "risk": 4.0},
        ]
        assert calc_trade_r_multiple(units, 108.0) == pytest.approx(3.5)

    def test_loss_is_negative(self):
        """止损出场：R = (96−100)/4 = −1"""
        r = calc_trade_r_multiple([{"price": 100.0, "shares": 100, "risk": 4.0}], 96.0)
        assert r == pytest.approx(-1.0)

    def test_zero_risk_unit_returns_none(self):
        """单位初始风险为 0（数据异常）→ 无法计算，返回 None"""
        assert calc_trade_r_multiple([{"price": 100.0, "shares": 100, "risk": 0.0}], 108.0) is None

    def test_empty_units_returns_none(self):
        assert calc_trade_r_multiple([], 108.0) is None


# ---------- 合成行情 cerebro 集成（离线） ----------

def _make_trend_df() -> pd.DataFrame:
    """合成日线：30 根横盘（构筑通道/ATR）→ 突破后稳步上涨（触发加仓）→ 急跌触发通道退出"""
    rows = []
    dates = pd.date_range("2025-01-01", periods=55, freq="B")
    for i, dt in enumerate(dates):
        if i < 30:
            close = 100.0
        elif i < 45:
            close = 101.0 + (i - 30) * 0.7  # 突破 100.6 通道高点，之后每根 +0.7（≈0.5N~1N，N≈1.2）
        else:
            close = 101.0 + 14 * 0.7 - (i - 44) * 8.0  # 急跌，跌破 10 日退出通道
        rows.append({
            "datetime": dt,
            "open": close,
            "high": close + 0.6,
            "low": close - 0.6,
            "close": close,
            "volume": 1_000_000,
        })
    return pd.DataFrame(rows).set_index("datetime")


class TestBacktestIntegration:
    def test_s1a_full_cycle_offline(self):
        """完整周期：突破入场 → 金字塔加仓 → 通道退出；R 按单位风险汇总且有值"""
        import backtrader as bt

        cerebro = bt.Cerebro()
        cerebro.broker.setcash(1_000_000)
        cerebro.adddata(bt.feeds.PandasData(dataname=_make_trend_df()))
        cerebro.addstrategy(S1A_Strategy)
        strat = cerebro.run()[0]

        assert len(strat.trade_results) == 1
        result = strat.trade_results[0]
        assert result["units"] >= 2  # 至少触发一次加仓
        assert result["r_multiple"] is not None
        assert isinstance(result["r_multiple"], float)
