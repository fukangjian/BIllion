"""
信号结算 ↔ 回测口径对账测试 — 同一合成行情下两者的止损口径一致性

口径说明（已知的模型差异，非缺陷）：
- signal_tracker 止损按「盘中最低价 ≤ 止损价」判定、按止损价结算（R=−1）；
- 回测按「收盘价 < 锁定止损」判定、下一根开盘成交；且通道退出判定先于止损。
因此两侧退出原因/价格可能不同，本测试锁定的是**共同口径**：
入场时锁定的固定止损价必须一致（信号收盘 − ATR_STOP_MULT × ATR），
且同行情下两者都退出、R 接近（≤0.3 容差）。

合成行情 TR 恒为 1.2 → ewm ATR（pipeline）与 Wilder 递归 ATR（回测）精确相等，
排除 ATR 实现差异对止损口径的干扰。全程离线（临时 SQLite + cerebro 合成数据）。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.database import get_connection, init_database, save_daily_quotes
from pipeline.signal_tracker import record_signals, settle_signals
from backtest.strategies import S1A_Strategy


def _make_reconcile_df() -> pd.DataFrame:
    """
    45 根合成日线：
    - 0-29：横盘 TR=1.2（高 100.6 / 低 99.4 / 收 100），20 日通道高 100.6，ATR=1.2
    - 30：突破收 101.0（TR 仍 1.2）→ 信号入场价 101.0，锁定止损 98.6
    - 31-39：小幅回落（低 99.7 > 98.6，不破止损；收盘 100.6 高于 10 日通道）
    - 40：低 97.9 / 收 98.0 —— 信号侧：低破止损 → 「止损」@98.6（R=−1）；
                                 回测侧：收破 10 日通道（99.7）→ 通道退出，41 根开盘 98.2 成交
    - 41+：阴跌收尾
    """
    rows = []
    dates = pd.date_range("2025-03-03", periods=45, freq="B")
    for i, dt in enumerate(dates):
        if i < 30:
            o, h, l, c = 100.0, 100.6, 99.4, 100.0
        elif i == 30:
            o, h, l, c = 100.2, 101.2, 100.0, 101.0
        elif i <= 39:
            o, h, l, c = 100.8, 101.0, 99.7, 100.6
        elif i == 40:
            o, h, l, c = 100.0, 100.2, 97.9, 98.0
        else:
            c = 98.0 - (i - 41) * 0.3
            o = 98.2 if i == 41 else c + 0.2
            h, l = o + 0.4, c - 0.3
        rows.append({"trade_date": dt.strftime("%Y-%m-%d"), "open": o, "high": h,
                     "low": l, "close": c, "volume": 1_000_000.0, "amount": 1e8})
    return pd.DataFrame(rows)


class TestSignalBacktestReconcile:
    def test_locked_stop_consistent(self, tmp_path):
        df = _make_reconcile_df()
        signal_date = df.iloc[30]["trade_date"]
        signal_close = float(df.iloc[30]["close"])  # 101.0

        # —— 信号侧：入库 → 结算 ——
        db = tmp_path / "market.db"
        init_database(db)
        q = df.copy()
        q["symbol"] = "600519"
        save_daily_quotes(q[["symbol", "trade_date", "open", "high", "low",
                             "close", "volume", "amount"]], db)
        n = record_signals(
            [{"symbol": "600519", "close": signal_close, "atr_20": 1.2, "period": 20}],
            system="S1-A", signal_date=signal_date, db_path=db,
        )
        assert n == 1
        result = settle_signals(db_path=db, settle_date="2026-01-01")
        assert result["settled"] == 1
        sig = result["details"][0]
        assert sig["exit_reason"] == "止损"
        assert sig["r_multiple"] == pytest.approx(-1.0)
        with get_connection(db) as conn:
            stop = conn.execute("SELECT stop_price FROM signals").fetchone()[0]
        assert stop == pytest.approx(98.6)  # 101.0 − 2×1.2

        # —— 回测侧：同数据 cerebro ——
        import backtrader as bt

        captured_stops = []

        class _Probe(S1A_Strategy):
            def notify_order(self, order):
                super().notify_order(order)
                if order.status == order.Completed and order.isbuy():
                    captured_stops.append(self.unit_positions[-1]["stop"])

        bt_df = df.rename(columns={"trade_date": "datetime"})
        bt_df["datetime"] = pd.to_datetime(bt_df["datetime"])
        bt_df = bt_df.set_index("datetime")
        cerebro = bt.Cerebro()
        cerebro.broker.setcash(1_000_000)
        cerebro.adddata(bt.feeds.PandasData(dataname=bt_df))
        cerebro.addstrategy(_Probe)
        strat = cerebro.run()[0]

        # 共同口径：锁定止损一致（98.6）；同行情下同样退出且 R 接近
        assert captured_stops[0] == pytest.approx(stop, abs=0.01)
        assert len(strat.trade_results) == 1
        bt_r = strat.trade_results[0]["r_multiple"]
        assert bt_r is not None and bt_r < 0
        assert abs(bt_r - sig["r_multiple"]) <= 0.3
