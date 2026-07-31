"""
持仓与风险视图 — 从 trades.json 读取未平仓交易，计算敞口与未实现盈亏
"""
import logging
from pathlib import Path
from typing import Optional

from config import ACCOUNT_EQUITY
from pipeline.database import load_daily_quotes
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


def get_latest_price(symbol: str, db_path: Optional[Path] = None) -> tuple[Optional[float], Optional[str]]:
    """从 market.db 读取该股最新收盘价与数据日期，无数据返回 (None, None)"""
    df = load_daily_quotes(symbol=_normalize_symbol(symbol), db_path=db_path)
    if df.empty:
        return None, None
    latest = df.sort_values("trade_date").iloc[-1]
    return float(latest["close"]), str(latest["trade_date"])


def _calc_unrealized_pnl(trade: Trade, db_path: Optional[Path] = None) -> Optional[dict]:
    """
    估算未实现盈亏（以 market.db 最新收盘价为市价源）。

    返回 {"pnl": 盈亏金额, "floating_r": 浮动R, "price": 现价, "data_date": 数据日期}；
    已平仓、入场价无效或 DB 无该股行情时返回 None（由调用方标注）。
    """
    if trade.is_closed or trade.入场价 <= 0:
        return None
    price, data_date = get_latest_price(trade.股票代码, db_path=db_path)
    if price is None:
        return None
    pnl = (price - trade.入场价) * trade.股数
    risk_per_share = trade.入场价 - trade.止损价
    floating_r = round((price - trade.入场价) / risk_per_share, 2) if risk_per_share > 0 else None
    return {"pnl": pnl, "floating_r": floating_r, "price": price, "data_date": data_date}


def print_portfolio_summary(
    trade_log: TradeLog | None = None,
    account_equity: float = ACCOUNT_EQUITY,
    db_path: Optional[Path] = None,
) -> None:
    """打印当前持仓摘要（股票、账户类型、风险%、浮动R、盈亏、回撤状态）"""
    log = trade_log or TradeLog()
    open_trades = log.list_all(open_only=True)

    if not open_trades:
        print("暂无持仓")
    else:
        print(f"{'代码':<8} {'名称':<10} {'账户':<8} {'风险%':<8} {'仓位%':<8} {'浮动R':<8} {'盈亏':<12}")
        print("-" * 66)
        data_dates: list[str] = []
        missing = 0
        for t in open_trades:
            pos_pct = t.仓位金额 / account_equity * 100 if account_equity > 0 else 0.0
            info = _calc_unrealized_pnl(t, db_path=db_path)
            if info is not None:
                pnl_str = f"{info['pnl']:+.2f}"
                r_str = f"{info['floating_r']:+.2f}" if info["floating_r"] is not None else "-"
                if info["data_date"]:
                    data_dates.append(info["data_date"])
            else:
                pnl_str = "-"
                r_str = "-"
                missing += 1
            print(
                f"{_normalize_symbol(t.股票代码):<8} "
                f"{t.股票名称:<10} "
                f"{t.账户类型:<8} "
                f"{t.风险率:<8.2f} "
                f"{pos_pct:<8.1f} "
                f"{r_str:<8} "
                f"{pnl_str:<12}"
            )
        print("-" * 66)
        print(f"合计风险敞口: {get_total_risk(log):.2f}%")
        if data_dates:
            print(f"市价数据日期: {max(data_dates)}（来源 market.db 最新收盘）")
        if missing:
            print(f"注: {missing} 只持仓在 market.db 无行情，盈亏未计算")

    # 回撤状态（推导值为准，失败降级不影响持仓表输出）
    try:
        from review.monitor import derive_drawdown_state

        dd = derive_drawdown_state(log, db_path=db_path)
        line = (
            f"回撤状态: {dd['state']}（推导回撤 {dd['drawdown_pct']}%，"
            f"当月已实现R {dd['monthly_realized_r']}）"
        )
        if dd.get("conflict"):
            line += f" ⚠️ 与环境变量 DRAWDOWN_STATE={dd['env']} 冲突，推导值为准，请人工确认"
        print(line)
        flags = dd.get("monthly_flags", {})
        if flags.get("event_trade_halt"):
            print("⚠️ 月度回撤触线：停止事件交易")
        if flags.get("new_position_halt"):
            print("⚠️ 月度回撤触线：停止开新仓")
    except Exception as e:
        print(f"回撤状态推导失败（已降级）: {e}")
