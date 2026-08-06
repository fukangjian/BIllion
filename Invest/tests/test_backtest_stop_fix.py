"""
回测止损口径回归测试 — 单位止损在入场时锁定（信号收盘 − 2N），不随 ATR 漂移

回归背景：原 BaseBreakoutStrategy.next 每根 bar 用「当前 ATR」重算止损，
波动放大时止损被动放宽，与 signal_tracker 信号结算、实盘持仓监控的
「入场时锁定的固定止损」口径不一致。2026-08 统一为锁定口径。
合成行情离线运行 cerebro，不连 AkShare、不读真实 market.db。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.strategies import S1A_Strategy, calc_trade_r_multiple


def _make_stop_test_df() -> pd.DataFrame:
    """
    合成日线（45 根）：
    - 0-29：恒定波幅横盘（TR=1.2），20 日通道高点 100.6，ATR 精确等于 1.2
    - 30：突破收盘 101.0（TR 仍为 1.2）→ 入场信号，锁定止损 = 101.0 − 2×1.2 = 98.6
    - 31-40：宽幅震荡（TR≈3.3）微涨，ATR 逐渐放大到 ≈2.1 —— 漂移口径下止损会放宽到 ≈96.8；
            第 35 根长下影（低点 97.0）压低 10 日退出通道，排除通道退出干扰
    - 41：收盘 98.0 —— 低于锁定止损 98.6，但高于漂移止损 ≈96.8，且未破 10 日通道 97.0
    - 42+：止损卖单以 97.5 成交后阴跌收尾
    """
    rows = []
    dates = pd.date_range("2025-03-02", periods=45, freq="B")
    for i, dt in enumerate(dates):
        if i < 30:
            o, h, l, c = 100.0, 100.6, 99.4, 100.0
        elif i == 30:
            o, h, l, c = 100.2, 101.2, 100.0, 101.0
        elif i <= 40:
            c = 101.2 + (i - 31) * 0.02
            o = c - 0.1
            h, l = c + 3.0, c - 0.3
            if i == 35:
                l = 97.0
        elif i == 41:
            o, h, l, c = 100.5, 100.8, 97.8, 98.0
        else:
            c = 97.3 - (i - 42) * 0.5
            o = c + 0.2
            h, l = o + 0.3, c - 0.3
        rows.append({
            "datetime": dt, "open": o, "high": h, "low": l, "close": c,
            "volume": 1_000_000,
        })
    return pd.DataFrame(rows).set_index("datetime")


class TestLockedStopLoss:
    def test_stop_locked_at_entry_not_drifting(self):
        """ATR 放大后价格落入 (漂移止损, 锁定止损] 区间：锁定口径必须出场，漂移口径不会"""
        import backtrader as bt

        captured_stops = []

        class _Probe(S1A_Strategy):
            def notify_order(self, order):
                super().notify_order(order)
                if order.status == order.Completed and order.isbuy():
                    captured_stops.append(self.unit_positions[-1]["stop"])

        cerebro = bt.Cerebro()
        cerebro.broker.setcash(1_000_000)
        cerebro.adddata(bt.feeds.PandasData(dataname=_make_stop_test_df()))
        cerebro.addstrategy(_Probe)
        strat = cerebro.run()[0]

        # 单位止损在入场时锁定：信号收盘 101.0 − 2 × ATR 1.2 = 98.6
        assert captured_stops[0] == pytest.approx(98.6, abs=0.01)
        # 收盘 98.0 触发锁定止损 → 整笔交易结束（漂移口径下此处不会出场，trade_results 为空）
        assert len(strat.trade_results) == 1
        r = strat.trade_results[0]["r_multiple"]
        assert r is not None
        assert r < 0  # 止损出场为亏损


class TestPerUnitExitPrice:
    def test_unit_exit_price_overrides_shared_exit(self):
        """止损分批成交的单位用各自 exit_price，其余单位用整笔退出价：
        (96−100)/4 + (110−102)/4 = −1.0 + 2.0 = 1.0"""
        units = [
            {"price": 100.0, "shares": 100, "risk": 4.0, "stop": 96.0, "exit_price": 96.0},
            {"price": 102.0, "shares": 100, "risk": 4.0, "stop": 98.0},
        ]
        assert calc_trade_r_multiple(units, 110.0) == pytest.approx(1.0)

    def test_units_without_exit_price_backward_compatible(self):
        """无 exit_price 的旧式单位仍按整笔退出价计算（向后兼容）"""
        units = [{"price": 100.0, "shares": 100, "risk": 4.0, "stop": 96.0}]
        assert calc_trade_r_multiple(units, 108.0) == pytest.approx(2.0)
