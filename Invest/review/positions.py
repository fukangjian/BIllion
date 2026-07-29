"""
持仓与风险视图 — 从 trades.json 读取未平仓交易，计算敞口
"""
import logging
from typing import Optional

from config import ACCOUNT_EQUITY
from review.trade_log import Trade, TradeLog

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    """标准化股票代码为 6 位"""
    return str(symbol).zfill(6)[-6:]


def export_open_positions(trade_log: TradeLog | None = None) -> list[dict]:
    """从 trades.json 读取未平仓交易，返回字典列表"""
    log = trade_log or TradeLog()
    open_trades = log.list_all(open_only=True)
    return [t.to_dict() for t in open_trades]


def get_current_exposure(
    industry: str,
    trade_log: TradeLog | None = None,
    account_equity: float = ACCOUNT_EQUITY,
) -> float:
    """计算指定产业/风险簇的当前敞口（占权益百分比）"""
    if account_equity <= 0:
        return 0.0

    log = trade_log or TradeLog()
    open_trades = log.list_all(open_only=True)
    total = 0.0
    for t in open_trades:
        if t.风险簇 == industry:
            total += t.仓位金额
    return total / account_equity * 100


def get_total_risk(
    trade_log: TradeLog | None = None,
) -> float:
    """计算未平仓交易的总风险敞口（风险率合计，百分比）"""
    log = trade_log or TradeLog()
    open_trades = log.list_all(open_only=True)
    return sum(t.风险率 for t in open_trades)


def get_position_for_watchlist(trade_log: TradeLog | None = None) -> list[str]:
    """返回当前持仓的股票代码列表（去重，保持插入顺序）"""
    log = trade_log or TradeLog()
    open_trades = log.list_all(open_only=True)
    seen: set[str] = set()
    symbols: list[str] = []
    for t in open_trades:
        sym = _normalize_symbol(t.股票代码)
        if sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols


def _calc_unrealized_pnl(trade: Trade) -> Optional[float]:
    """估算未实现盈亏（无市价时返回 None）"""
    if trade.is_closed or trade.入场价 <= 0:
        return None
    # 未平仓且无退出价，暂无市价数据源，返回 None
    return None


def print_portfolio_summary(
    trade_log: TradeLog | None = None,
    account_equity: float = ACCOUNT_EQUITY,
) -> None:
    """打印当前持仓摘要（股票、账户类型、风险%、盈亏）"""
    log = trade_log or TradeLog()
    open_trades = log.list_all(open_only=True)

    if not open_trades:
        print("暂无持仓")
        return

    print(f"{'代码':<8} {'名称':<10} {'账户':<8} {'风险%':<8} {'仓位%':<8} {'盈亏':<10}")
    print("-" * 58)
    for t in open_trades:
        pos_pct = t.仓位金额 / account_equity * 100 if account_equity > 0 else 0.0
        pnl = _calc_unrealized_pnl(t)
        pnl_str = f"{pnl:+.2f}" if pnl is not None else "-"
        print(
            f"{_normalize_symbol(t.股票代码):<8} "
            f"{t.股票名称:<10} "
            f"{t.账户类型:<8} "
            f"{t.风险率:<8.2f} "
            f"{pos_pct:<8.1f} "
            f"{pnl_str:<10}"
        )
    print("-" * 58)
    print(f"合计风险敞口: {get_total_risk(log):.2f}%")
