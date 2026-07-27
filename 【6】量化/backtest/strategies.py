"""
Backtrader 策略定义 — S1-A 快速系统 & S2-A 慢速系统

S1-A: 20日通道突破入场，10日通道退出，ATR(20) 仓位管理
S2-A: 55日通道突破入场，20日通道退出，ATR(20) 仓位管理
加仓规则: 0.5N-1N 间距
"""
import backtrader as bt
import numpy as np


class DonchianHigh(bt.Indicator):
    """Donchian 通道上轨（不含当前 bar）"""
    lines = ("donchian_high",)
    params = (("period", 20),)

    def __init__(self):
        self.addminperiod(self.params.period + 1)

    def next(self):
        highs = [self.data.high[-i] for i in range(1, self.params.period + 1)]
        self.lines.donchian_high[0] = max(highs)


class DonchianLow(bt.Indicator):
    """Donchian 通道下轨（不含当前 bar）"""
    lines = ("donchian_low",)
    params = (("period", 20),)

    def __init__(self):
        self.addminperiod(self.params.period + 1)

    def next(self):
        lows = [self.data.low[-i] for i in range(1, self.params.period + 1)]
        self.lines.donchian_low[0] = min(lows)


class ATRIndicator(bt.Indicator):
    """ATR(N) — Wilder 平滑"""
    lines = ("atr",)
    params = (("period", 20),)

    def __init__(self):
        self.addminperiod(self.params.period + 1)

    def next(self):
        period = self.params.period
        if len(self) == period + 1:
            trs = []
            for i in range(1, period + 1):
                h = self.data.high[-i]
                l = self.data.low[-i]
                pc = self.data.close[-i - 1] if len(self) > i else self.data.close[-i]
                tr = max(h - l, abs(h - pc), abs(l - pc))
                trs.append(tr)
            self.lines.atr[0] = sum(trs) / period
        else:
            h = self.data.high[0]
            l = self.data.low[0]
            pc = self.data.close[-1]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            prev_atr = self.lines.atr[-1]
            self.lines.atr[0] = (prev_atr * (period - 1) + tr) / period


class BaseBreakoutStrategy(bt.Strategy):
    """
    突破策略基类 — ATR 仓位管理 + 金字塔加仓
    """
    params = (
        ("entry_period", 20),       # 入场通道周期
        ("exit_period", 10),        # 退出通道周期
        ("atr_period", 20),         # ATR 周期
        ("risk_pct", 0.005),        # 单笔风险比例 0.5%
        ("atr_stop_mult", 2.0),     # 止损 = 2N
        ("add_spacing_min", 0.5),   # 加仓最小间距 0.5N
        ("add_spacing_max", 1.0),   # 加仓最大间距 1N
        ("max_units", 4),           # 最大加仓单位
        ("printlog", False),
    )

    def __init__(self):
        self.entry_high = DonchianHigh(self.data, period=self.params.entry_period)
        self.exit_low = DonchianLow(self.data, period=self.params.exit_period)
        self.atr = ATRIndicator(self.data, period=self.params.atr_period)

        self.order = None
        self.entry_price = 0.0
        self.units = 0
        self.last_add_price = 0.0
        self.trade_results = []  # 记录每笔交易 R 倍数

    def log(self, txt, dt=None):
        if self.params.printlog:
            dt = dt or self.datas[0].datetime.date(0)
            print(f"{dt.isoformat()} {txt}")

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return
        if order.status == order.Completed:
            if order.isbuy():
                self.log(f"买入 {order.executed.size} @ {order.executed.price:.2f}")
            else:
                self.log(f"卖出 {order.executed.size} @ {order.executed.price:.2f}")
        elif order.status in (order.Canceled, order.Margin, order.Rejected):
            self.log("订单取消/拒绝")
        self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            pnl = trade.pnlcomm
            # 估算 R 倍数
            n = self.atr[0] if self.atr[0] > 0 else 1
            r_multiple = pnl / (n * 100) if n > 0 else 0
            self.trade_results.append({
                "pnl": pnl,
                "r_multiple": r_multiple,
                "bars": trade.barlen,
            })
            self.log(f"交易结束 PnL={pnl:.2f} R={r_multiple:.2f}")

    def _calc_unit_size(self) -> int:
        """基于 ATR 计算一个单位的股数"""
        n = self.atr[0]
        if n <= 0:
            return 0
        equity = self.broker.getvalue()
        risk_amount = equity * self.params.risk_pct
        stop_distance = n * self.params.atr_stop_mult
        if stop_distance <= 0:
            return 0
        raw = risk_amount / stop_distance
        # A股100股一手
        size = int(raw // 100) * 100
        return max(size, 100)

    def next(self):
        if self.order:
            return

        if len(self) < self.params.entry_period + 5:
            return

        close = self.data.close[0]
        n = self.atr[0]

        if not self.position:
            # 入场：突破 entry_period 日高点
            if close > self.entry_high[0] and self.entry_high[0] > 0:
                size = self._calc_unit_size()
                if size > 0:
                    self.order = self.buy(size=size)
                    self.entry_price = close
                    self.last_add_price = close
                    self.units = 1
                    self.log(f"突破入场 size={size} close={close:.2f} channel={self.entry_high[0]:.2f}")
        else:
            # 退出：跌破 exit_period 日低点
            if close < self.exit_low[0] and self.exit_low[0] > 0:
                self.order = self.close()
                self.units = 0
                self.log(f"通道退出 close={close:.2f} channel={self.exit_low[0]:.2f}")
                return

            # ATR 止损
            stop_price = self.entry_price - n * self.params.atr_stop_mult
            if close < stop_price:
                self.order = self.close()
                self.units = 0
                self.log(f"ATR止损 close={close:.2f} stop={stop_price:.2f}")
                return

            # 金字塔加仓: 0.5N-1N 间距
            if self.units < self.params.max_units and n > 0:
                add_distance = close - self.last_add_price
                min_spacing = n * self.params.add_spacing_min
                max_spacing = n * self.params.add_spacing_max
                if add_distance >= min_spacing:
                    size = self._calc_unit_size()
                    if size > 0:
                        self.order = self.buy(size=size)
                        self.last_add_price = close
                        self.units += 1
                        self.log(f"加仓 #{self.units} size={size} @ {close:.2f}")


class S1A_Strategy(BaseBreakoutStrategy):
    """
    S1-A 快速系统
    20日通道突破入场，10日通道退出
    """
    params = (
        ("entry_period", 20),
        ("exit_period", 10),
        ("atr_period", 20),
        ("risk_pct", 0.005),
        ("atr_stop_mult", 2.0),
        ("add_spacing_min", 0.5),
        ("add_spacing_max", 1.0),
        ("max_units", 4),
        ("printlog", False),
    )


class S2A_Strategy(BaseBreakoutStrategy):
    """
    S2-A 慢速系统
    55日通道突破入场，20日通道退出
    """
    params = (
        ("entry_period", 55),
        ("exit_period", 20),
        ("atr_period", 20),
        ("risk_pct", 0.005),
        ("atr_stop_mult", 2.0),
        ("add_spacing_min", 0.5),
        ("add_spacing_max", 1.0),
        ("max_units", 4),
        ("printlog", False),
    )


STRATEGY_MAP = {
    "S1-A": S1A_Strategy,
    "S2-A": S2A_Strategy,
    "s1a": S1A_Strategy,
    "s2a": S2A_Strategy,
}
