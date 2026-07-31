"""
指标计算 — R倍数、MFE/MAE、累计统计、分组统计
"""
import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from review.trade_log import Trade

logger = logging.getLogger(__name__)


def calc_r_multiple(entry: float, exit_price: float, stop: float) -> Optional[float]:
    if entry <= 0 or stop <= 0 or exit_price is None:
        return None
    risk_per_share = entry - stop
    if abs(risk_per_share) < 1e-6:
        return None
    return (exit_price - entry) / risk_per_share


def calc_mfe_mae_from_prices(
    entry: float,
    stop: float,
    high: float,
    low: float,
) -> tuple[Optional[float], Optional[float]]:
    if entry <= 0 or stop <= 0:
        return None, None
    risk = entry - stop
    if abs(risk) < 1e-6:
        return None, None
    mfe = (high - entry) / risk
    mae = (low - entry) / risk
    return round(mfe, 2), round(mae, 2)


def fetch_price_range(symbol: str, start_date: str, end_date: str) -> tuple[Optional[float], Optional[float]]:
    from shared.data_fetcher import fetch_stock_daily

    symbol = symbol.zfill(6)[-6:]

    try:
        # 与数据管道共用双源获取（新浪优先，东方财富备用），避免单一数据源不可用
        df = fetch_stock_daily(symbol, start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return None, None
        high = float(df["high"].max())
        low = float(df["low"].min())
        return high, low
    except Exception as e:
        logger.warning("获取 %s 价格区间失败: %s", symbol, e)
        return None, None


def enrich_trade_metrics(trade: Trade, fetch_prices: bool = True) -> Trade:
    if trade.is_closed and trade.实际退出价 is not None:
        trade.R倍数 = calc_r_multiple(trade.入场价, trade.实际退出价, trade.止损价)
        if trade.R倍数 is not None:
            trade.R倍数 = round(trade.R倍数, 2)

    if fetch_prices and trade.退出日期:
        high, low = fetch_price_range(trade.股票代码, trade.日期, trade.退出日期)
        if high is not None and low is not None:
            mfe, mae = calc_mfe_mae_from_prices(trade.入场价, trade.止损价, high, low)
            trade.MFE = mfe
            trade.MAE = mae

    return trade


@dataclass
class TradeStats:
    总交易笔数: int = 0
    已平仓笔数: int = 0
    系统内笔数: int = 0
    系统内占比: float = 0.0
    胜率: float = 0.0
    平均盈利R: float = 0.0
    平均亏损R: float = 0.0
    期望值: float = 0.0
    profit_factor: float = 0.0
    最大连续亏损: int = 0
    最大回撤R: float = 0.0
    总盈利R: float = 0.0
    总亏损R: float = 0.0
    净R: float = 0.0


def compute_stats(trades: list[Trade], fetch_prices: bool = False) -> TradeStats:
    stats = TradeStats()
    if not trades:
        return stats

    stats.总交易笔数 = len(trades)
    stats.系统内笔数 = sum(1 for t in trades if t.是否系统内交易)
    stats.系统内占比 = round(stats.系统内笔数 / stats.总交易笔数 * 100, 1) if stats.总交易笔数 else 0

    closed = [t for t in trades if t.is_closed]
    stats.已平仓笔数 = len(closed)

    if not closed:
        return stats

    r_values = []
    for t in closed:
        if fetch_prices:
            enrich_trade_metrics(t, fetch_prices=True)
        r = t.R倍数
        if r is None:
            r = calc_r_multiple(t.入场价, t.实际退出价, t.止损价)
        if r is not None:
            r_values.append((t, r))

    if not r_values:
        return stats

    wins = [r for _, r in r_values if r > 0]
    losses = [r for _, r in r_values if r <= 0]

    stats.胜率 = round(len(wins) / len(r_values) * 100, 1)
    stats.平均盈利R = round(sum(wins) / len(wins), 2) if wins else 0.0
    stats.平均亏损R = round(sum(losses) / len(losses), 2) if losses else 0.0
    stats.期望值 = round(
        (stats.胜率 / 100) * stats.平均盈利R + (1 - stats.胜率 / 100) * stats.平均亏损R,
        2,
    )

    total_profit = sum(r for r in wins)
    total_loss = abs(sum(r for r in losses))
    stats.总盈利R = round(total_profit, 2)
    stats.总亏损R = round(-total_loss, 2) if losses else 0.0
    stats.净R = round(total_profit + sum(r for r in losses), 2)
    stats.profit_factor = round(total_profit / total_loss, 2) if total_loss > 0 else float("inf")

    max_streak = 0
    current_streak = 0
    for _, r in r_values:
        if r <= 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0
    stats.最大连续亏损 = max_streak

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for _, r in r_values:
        cumulative += r
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)
    stats.最大回撤R = round(max_dd, 2)

    return stats


def group_by_strategy(trades: list[Trade]) -> pd.DataFrame:
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        key = t.入场系统 or "未分类"
        groups.setdefault(key, []).append(t)

    rows = []
    for strategy, group in groups.items():
        s = compute_stats(group)
        rows.append({
            "策略": strategy,
            "交易数": s.总交易笔数,
            "已平仓": s.已平仓笔数,
            "胜率(%)": s.胜率,
            "平均盈利R": s.平均盈利R,
            "平均亏损R": s.平均亏损R,
            "期望值": s.期望值,
            "Profit Factor": s.profit_factor if s.profit_factor != float("inf") else "∞",
            "净R": s.净R,
        })
    return pd.DataFrame(rows)


def group_by_account(trades: list[Trade]) -> pd.DataFrame:
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        key = t.账户类型 or "未分类"
        groups.setdefault(key, []).append(t)

    rows = []
    for account, group in groups.items():
        s = compute_stats(group)
        rows.append({
            "账户类型": account,
            "交易数": s.总交易笔数,
            "系统内占比(%)": s.系统内占比,
            "胜率(%)": s.胜率,
            "期望值": s.期望值,
            "净R": s.净R,
        })
    return pd.DataFrame(rows)


def stats_to_markdown(stats: TradeStats) -> str:
    pf = f"{stats.profit_factor:.2f}" if stats.profit_factor != float("inf") else "∞"
    lines = [
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 总交易笔数 | {stats.总交易笔数} |",
        f"| 已平仓笔数 | {stats.已平仓笔数} |",
        f"| 系统内占比 | {stats.系统内占比}% |",
        f"| 胜率 | {stats.胜率}% |",
        f"| 平均盈利 R | {stats.平均盈利R} |",
        f"| 平均亏损 R | {stats.平均亏损R} |",
        f"| 期望值 | {stats.期望值} |",
        f"| Profit Factor | {pf} |",
        f"| 最大连续亏损 | {stats.最大连续亏损} 笔 |",
        f"| 最大回撤 R | {stats.最大回撤R} |",
        f"| 净 R | {stats.净R} |",
    ]
    return "\n".join(lines)
