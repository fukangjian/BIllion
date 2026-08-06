"""
Backtrader 策略定义 — S1-A 快速系统 & S2-A 慢速系统

S1-A: 20日通道突破入场，10日通道退出，ATR(20) 仓位管理
S2-A: 55日通道突破入场，20日通道退出，ATR(20) 仓位管理
加仓规则: 每涨 0.5N 加一单位；单根跳空超过 1N 不追（跳过的单位不补）
止损口径: 每个单位入场时锁定固定止损（信号收盘 − 2N），不随 ATR 漂移，
          与 signal_tracker 信号结算、实盘持仓监控口径一致

策略参数统一来自 config（STRATEGY_PARAMS / ATR_STOP_MULT / ADD_SPACING_* 等），
与投资体系 V5.0 §4.3 口径一致，勿在本文件硬编码数值。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import backtrader as bt
import numpy as np

from config import (
    ADD_SPACING_MAX,
    ADD_SPACING_MIN,
    ATR_PERIOD,
    ATR_STOP_MULT,
    BACKTEST_RISK_PCT,
    LOT_SIZE,
    MAX_UNITS,
    STRATEGY_PARAMS,
)


def next_add_action(
    close: float,
    last_add_price: float,
    n: float,
    spacing_min: float = ADD_SPACING_MIN,
    spacing_max: float = ADD_SPACING_MAX,
) -> tuple[bool, float]:
    """
    金字塔加仓判定（海龟法则口径，回测/实盘同一标准）：

    - 间距在 [0.5N, 1N]：(True, close) —— 按当前价加仓一单位；
    - 间距 > 1N：(False, 新基准价) —— 跳空不追，基准价推进到最近一个被跳过的
      0.5N 档位，跳过的单位不补，等回踩或价格继续触发下一档；
    - 间距 < 0.5N：(False, last_add_price) —— 未达间距，不加仓。
    """
    add_distance = close - last_add_price
    min_spacing = n * spacing_min
    max_spacing = n * spacing_max
    if min_spacing <= add_distance <= max_spacing:
        return True, close
    if add_distance > max_spacing:
        skipped = int(add_distance // min_spacing)
        return False, last_add_price + skipped * min_spacing
    return False, last_add_price


def calc_trade_r_multiple(units: list[dict], exit_price: float) -> float | None:
    """
    按单位真实初始风险计算整笔交易的 R 倍数：

    每单位 R = (单位卖出价 − 单位入场价) / 单位初始风险
    （单位初始风险 = 单位入场价 − 单位初始止损价 = 下单时 ATR × atr_stop_mult），
    多单位分别计算后汇总求和；无有效单位时返回 None。
    单位可自带 exit_price（止损分批成交时各自的实际退出价），缺省用整笔退出价。
    """
    r_values = []
    for u in units:
        if u.get("risk", 0) <= 0:
            continue
        ep = u.get("exit_price")
        if ep is None:
            ep = exit_price
        r_values.append((ep - u["price"]) / u["risk"])
    if not r_values:
        return None
    return round(sum(r_values), 2)


class DonchianHigh(bt.Indicator):
    """Donchian 通道上轨（不含当前 bar）"""
    lines = ("donchian_high",)
    params = (("period", STRATEGY_PARAMS["S1-A"]["entry_channel"]),)

    def __init__(self):
        self.addminperiod(self.params.period + 1)

    def next(self):
        highs = [self.data.high[-i] for i in range(1, self.params.period + 1)]
        self.lines.donchian_high[0] = max(highs)


class DonchianLow(bt.Indicator):
    """Donchian 通道下轨（不含当前 bar）"""
    lines = ("donchian_low",)
    params = (("period", STRATEGY_PARAMS["S1-A"]["exit_channel"]),)

    def __init__(self):
        self.addminperiod(self.params.period + 1)

    def next(self):
        lows = [self.data.low[-i] for i in range(1, self.params.period + 1)]
        self.lines.donchian_low[0] = min(lows)


class ATRIndicator(bt.Indicator):
    """ATR(N) — Wilder 平滑"""
    lines = ("atr",)
    params = (("period", ATR_PERIOD),)

    def __init__(self):
        self.addminperiod(self.params.period + 1)

    def next(self):
        period = self.params.period
        if len(self) == period + 1:
            trs = []
            for i in range(1, period + 1):
                h = self.data.high[-i]
                l = self.data.low[-i]
                # 前收盘在序列起点不可用时回退当根收盘（len > i+1 才允许读 close[-i-1]，
                # 否则越界读到脏值污染 SMA 种子）
                pc = self.data.close[-i - 1] if len(self) > i + 1 else self.data.close[-i]
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

    参数默认值全部取自 config（见文件头部 import）；子类只需覆盖 entry/exit 通道周期。
    """
    params = (
        ("entry_period", STRATEGY_PARAMS["S1-A"]["entry_channel"]),  # 入场通道周期
        ("exit_period", STRATEGY_PARAMS["S1-A"]["exit_channel"]),    # 退出通道周期
        ("atr_period", ATR_PERIOD),               # ATR 周期
        ("risk_pct", BACKTEST_RISK_PCT),          # 单笔风险比例（回测口径，实盘由账户限额决定）
        ("atr_stop_mult", ATR_STOP_MULT),         # 初始止损 = 2N
        ("add_spacing_min", ADD_SPACING_MIN),     # 加仓最小间距 0.5N
        ("add_spacing_max", ADD_SPACING_MAX),     # 加仓间距上限 1N（跳空不追）
        ("max_units", MAX_UNITS),                 # 最大单位数
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
        self.unit_positions = []       # 持仓单位明细 [{price, shares, risk, stop, exit_price?}]，risk=单位初始风险(2N)
        self._pending_unit_risk = 0.0  # 在途买入订单对应的单位初始风险
        self._pending_unit_stop = 0.0  # 在途买入订单对应的单位锁定止损价
        self._pending_stop_units = []  # 在途止损卖单对应的单位（成交后回填各自退出价）
        self._last_exit_price = None   # 最近一次卖出成交价（notify_trade 算 R 用）
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
                # 记录持仓单位（单位初始风险 = 下单时 ATR × atr_stop_mult = 入场价 − 初始止损价；
                # stop 为下单时锁定的固定止损价，不随 ATR 漂移）
                self.unit_positions.append({
                    "price": order.executed.price,
                    "shares": order.executed.size,
                    "risk": self._pending_unit_risk,
                    "stop": self._pending_unit_stop,
                })
                self.log(f"买入 {order.executed.size} @ {order.executed.price:.2f}")
            else:
                self._last_exit_price = order.executed.price
                for u in self._pending_stop_units:
                    u["exit_price"] = order.executed.price
                self._pending_stop_units = []
                self.log(f"卖出 {order.executed.size} @ {order.executed.price:.2f}")
        elif order.status in (order.Canceled, order.Margin, order.Rejected):
            self.log("订单取消/拒绝")
        self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            pnl = trade.pnlcomm
            # R 倍数按单位真实初始风险分别计算再汇总（见 calc_trade_r_multiple）
            exit_price = self._last_exit_price or self.data.close[0]
            r_multiple = calc_trade_r_multiple(self.unit_positions, exit_price)
            self.trade_results.append({
                "pnl": pnl,
                "r_multiple": r_multiple,
                "units": len(self.unit_positions),
                "bars": trade.barlen,
            })
            self.unit_positions = []
            self._last_exit_price = None
            r_text = f"{r_multiple:.2f}" if r_multiple is not None else "N/A"
            self.log(f"交易结束 PnL={pnl:.2f} R={r_text}")

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
        # A股按整手交易
        size = int(raw // LOT_SIZE) * LOT_SIZE
        return max(size, LOT_SIZE)

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
                    self._pending_unit_risk = n * self.params.atr_stop_mult
                    self._pending_unit_stop = close - self._pending_unit_risk  # 锁定止损，不再随 ATR 重算
                    self.order = self.buy(size=size)
                    self.entry_price = close
                    self.last_add_price = close
                    self.units = 1
                    self.log(f"突破入场 size={size} close={close:.2f} channel={self.entry_high[0]:.2f}")
        else:
            # 退出：跌破 exit_period 日低点（全部单位一起退出）
            if close < self.exit_low[0] and self.exit_low[0] > 0:
                self.order = self.close()
                self.units = 0
                self.log(f"通道退出 close={close:.2f} channel={self.exit_low[0]:.2f}")
                return

            # ATR 止损：每个单位用各自入场时锁定的固定止损价（与 signal_tracker/实盘监控口径一致）；
            # 触及止损的单位合并为一单卖出，未触及的单位继续持有
            stopped = [u for u in self.unit_positions if u.get("exit_price") is None and close < u["stop"]]
            if stopped:
                self._pending_stop_units = stopped
                shares = sum(u["shares"] for u in stopped)
                self.units -= len(stopped)
                self.order = self.sell(size=shares)
                stop_desc = "/".join(f"{u['stop']:.2f}" for u in stopped)
                self.log(f"ATR止损 {len(stopped)} 个单位 close={close:.2f} stop={stop_desc}")
                return

            # 金字塔加仓: 0.5N~1N 间距（跳空超 1N 不追，跳过的单位不补）
            if self.units < self.params.max_units and n > 0:
                should_add, new_baseline = next_add_action(
                    close, self.last_add_price, n,
                    self.params.add_spacing_min, self.params.add_spacing_max,
                )
                if should_add:
                    size = self._calc_unit_size()
                    if size > 0:
                        self._pending_unit_risk = n * self.params.atr_stop_mult
                        self._pending_unit_stop = close - self._pending_unit_risk  # 加仓单位同样锁定止损
                        self.order = self.buy(size=size)
                        self.last_add_price = close
                        self.units += 1
                        self.log(f"加仓 #{self.units} size={size} @ {close:.2f}")
                elif new_baseline != self.last_add_price:
                    # 跳空超过 1N：本次不追，基准推进至最近被跳过的 0.5N 档位
                    self.last_add_price = new_baseline
                    self.log(f"跳空超 {self.params.add_spacing_max:g}N，加仓基准推进至 {new_baseline:.2f}")


class S1A_Strategy(BaseBreakoutStrategy):
    """
    S1-A 快速系统（通道周期见 config.STRATEGY_PARAMS["S1-A"]）
    20日通道突破入场，10日通道退出
    """
    params = (
        ("entry_period", STRATEGY_PARAMS["S1-A"]["entry_channel"]),
        ("exit_period", STRATEGY_PARAMS["S1-A"]["exit_channel"]),
    )


class S2A_Strategy(BaseBreakoutStrategy):
    """
    S2-A 慢速系统（通道周期见 config.STRATEGY_PARAMS["S2-A"]）
    55日通道突破入场，20日通道退出
    """
    params = (
        ("entry_period", STRATEGY_PARAMS["S2-A"]["entry_channel"]),
        ("exit_period", STRATEGY_PARAMS["S2-A"]["exit_channel"]),
    )


STRATEGY_MAP = {
    "S1-A": S1A_Strategy,
    "S2-A": S2A_Strategy,
    "s1a": S1A_Strategy,
    "s2a": S2A_Strategy,
}
