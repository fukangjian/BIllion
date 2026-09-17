"""
程序化交易操作层 — Web 控制台 API 与 CLI 共用的业务逻辑（返回 dict，不打印）

与 review/cli.py 的 cmd_* 命令同一业务口径（合规闸门 / force 留痕 / 买入卡 /
金字塔加仓 / 拆单卖出），区别仅在于这里返回结构化结果供 FastAPI 序列化。
所有函数支持注入 trade_log / db_path，测试一律用 tmp_path，不碰真实数据。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import logging
from datetime import datetime
from typing import Optional

from config import (
    ACCOUNT_EQUITY,
    APP_VERSION,
    ATR_STOP_MULT,
    MARKET_SCAN_OUTPUT_DIR,
    PORTFOLIO_HEAT_LIMITS,
    SYSTEM_DEFAULT_ACCOUNT,
)
from position_calculator import calc_position, lookup_cluster
from review.entry_gate import check_entry, derive_state_safe, force_note, get_latest_market_state, split_by_severity
from review.trade_log import Trade, TradeLog

logger = logging.getLogger(__name__)


# ---------- 查询 ----------

def get_overview(trade_log: TradeLog | None = None, db_path=None) -> dict:
    """控制台顶部状态：市场状态 / 回撤状态 / 权益 / 持仓数 / 总风险 / 今日信号计数"""
    log = trade_log or TradeLog()
    state_row: dict = {}
    try:
        from pipeline.database import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT trade_date, state, suggested_pos FROM market_state "
                "ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
        if row:
            state_row = {"data_date": row[0], "market_state": row[1], "suggested_pos": row[2]}
    except Exception as e:
        logger.warning("市场状态读取失败（降级）: %s", e)

    drawdown: dict = {"state": "Normal", "drawdown_pct": 0.0}
    try:
        from review.monitor import derive_drawdown_state

        drawdown = derive_drawdown_state(log, db_path=db_path)
    except Exception as e:
        logger.warning("回撤状态推导失败（降级 Normal）: %s", e)

    open_trades = log.list_all(open_only=True)
    signals = {"S1-A": 0, "S2-A": 0, "filters_all_passed": 0}
    scan = _load_latest_scan_json()
    if scan:
        for key, sysname in (("breakout_s1a", "S1-A"), ("breakout_s2a", "S2-A")):
            items = scan.get(key, [])
            signals[sysname] = len(items)
            signals["filters_all_passed"] += sum(
                1 for it in items
                if it.get("record", True)
                and it.get("filter_passed") is not None
                and it.get("filter_passed") == it.get("filters_required")
            )

    return {
        "version": APP_VERSION,
        "equity": ACCOUNT_EQUITY,
        "open_count": len(open_trades),
        "total_risk_pct": round(sum(t.风险率 for t in open_trades if t.风险率 > 0), 2),
        "drawdown_state": drawdown.get("state", "Normal"),
        "drawdown_pct": drawdown.get("drawdown_pct", 0.0),
        "signals": signals,
        **state_row,
    }


def get_daily_plan(db_path=None) -> dict:
    """最新扫描 JSON 中的明日操作计划（daily_plan 键）"""
    scan = _load_latest_scan_json()
    if not scan:
        return {"available": False, "note": "未找到扫描 JSON，请先运行盘前流程"}
    plan = scan.get("daily_plan")
    if not plan:
        return {"available": False, "date": scan.get("date"), "note": "该扫描无操作计划（旧格式或构建降级）"}
    return {"available": True, "date": scan.get("date"), "market_state": scan.get("market_state"), **plan}


def get_positions_view(trade_log: TradeLog | None = None, db_path=None, equity: float | None = None) -> dict:
    """持仓摘要（未实现盈亏/浮动R/风险敞口/账户热度条）"""
    from review.positions import _calc_unrealized_pnl

    log = trade_log or TradeLog()
    equity = equity or ACCOUNT_EQUITY
    open_trades = log.list_all(open_only=True)
    rows = []
    data_dates = []
    for t in open_trades:
        pnl = _calc_unrealized_pnl(t, db_path=db_path)
        if pnl and pnl.get("data_date"):
            data_dates.append(pnl["data_date"])
        rows.append({
            "交易编号": t.交易编号,
            "股票代码": t.股票代码,
            "股票名称": t.股票名称,
            "账户类型": t.账户类型,
            "入场系统": t.入场系统,
            "入场价": t.入场价,
            "止损价": t.止损价,
            "股数": t.股数,
            "风险率": t.风险率,
            "仓位比例": round(t.仓位金额 / equity * 100, 1) if equity > 0 else 0,
            "现价": pnl["price"] if pnl else None,
            "浮动R": pnl["floating_r"] if pnl else None,
            "未实现盈亏": round(pnl["pnl"], 0) if pnl else None,
        })

    total_risk = round(sum(t.风险率 for t in open_trades if t.风险率 > 0), 2)
    mstate = get_latest_market_state(db_path)
    heat_limit = PORTFOLIO_HEAT_LIMITS.get(str(mstate or "").upper())
    return {
        "positions": rows,
        "data_date": max(data_dates) if data_dates else None,
        "total_risk_pct": total_risk,
        "market_state": mstate,
        "heat_limit": heat_limit,
    }


def get_sell_check_lines(symbol: str, trade_log: TradeLog | None = None, db_path=None) -> dict:
    """卖点检查单（复用 cli.build_sell_check_lines 纯函数）返回文本行"""
    from review.cli import _symbol_in_hot_pool, build_sell_check_lines
    from review.monitor import _check_single_position

    log = trade_log or TradeLog()
    symbol = symbol.zfill(6)[-6:]
    candidates = [t for t in log.get_by_symbol(symbol) if not t.is_closed]
    if not candidates:
        return {"found": False, "lines": [f"{symbol} 当前无持仓中交易"]}
    trade = max(candidates, key=lambda t: (t.日期 or "", t.创建时间 or ""))
    try:
        position_info = _check_single_position(trade, db_path=db_path)
    except Exception as e:
        logger.warning("持仓检查失败（降级）: %s", e)
        position_info = None
    in_hot_pool = _symbol_in_hot_pool(symbol)
    close = position_info.get("现价") if position_info else None
    return {"found": True, "交易编号": trade.交易编号,
            "lines": build_sell_check_lines(trade, position_info, in_hot_pool, close)}


# ---------- 写操作（合规闸门 + 留痕，与 cli 同口径） ----------

def _gate(trade: Trade, log: TradeLog, equity: float, force: bool, state: str | None) -> dict:
    """合规闸门（API 版）：返回 {allowed, state, high, others, note}"""
    violations, state = check_entry(trade, trade_log=log, account_equity=equity, drawdown_state=state)
    high, others = split_by_severity(violations)
    result = {
        "state": state,
        "high": [f"{v.违规类型}: {v.描述}" for v in high],
        "others": [f"{v.违规类型}: {v.描述}" for v in others],
        "allowed": True,
        "note": "",
    }
    if high and not force:
        result["allowed"] = False
        return result
    if high and force:
        result["note"] = force_note(high)
    return result


def execute_add(
    symbol: str, account: str, system: str, entry: float, stop: float,
    risk: float, shares: int, name: str = "", cluster: str = "",
    force: bool = False, equity: float | None = None, trade_log: TradeLog | None = None,
    target: float | None = None, logic: str = "",
) -> dict:
    """手工建仓（对应 cli add）：构造 Trade → 闸门 → 落库 → 买入卡"""
    from review.buy_card import calc_dict_from_trade, generate_buy_card

    log = trade_log or TradeLog()
    equity = equity or ACCOUNT_EQUITY
    resolved_cluster = cluster or lookup_cluster(symbol) or "未指定"
    trade = Trade(
        日期=datetime.now().strftime("%Y-%m-%d"),
        股票代码=symbol, 股票名称=name, 账户类型=account, 风险簇=resolved_cluster,
        入场系统=system, 核心逻辑=logic, 入场价=entry, 止损价=stop, 风险率=risk,
        股数=shares, 仓位金额=round(entry * shares, 2), 目标价=target, 是否系统内交易=True,
    )
    gate = _gate(trade, log, equity, force, state=None)
    if not gate["allowed"]:
        return {"ok": False, "reason": "高级违规拒绝", **gate}
    if gate["note"]:
        trade.备注 = gate["note"]
    log.add(trade)
    card = generate_buy_card(trade, calc_dict_from_trade(trade, equity))
    return {"ok": True, "交易编号": trade.交易编号, "买入卡": str(card) if card else None, **gate}


def execute_from_scan(
    symbol: str, system: str | None = None, account: str | None = None,
    force: bool = False, equity: float | None = None,
    trade_log: TradeLog | None = None, scan_data: dict | None = None,
) -> dict:
    """从最新扫描信号一键建仓（对应 cli from-scan --execute）"""
    from review.buy_card import generate_buy_card
    from review.cli import _default_account_for_system

    log = trade_log or TradeLog()
    equity = equity or ACCOUNT_EQUITY
    symbol = symbol.zfill(6)[-6:]
    scan_data = scan_data or _load_latest_scan_json()
    if not scan_data:
        return {"ok": False, "reason": "未找到扫描 JSON，请先运行盘前流程"}

    found = {}
    for key, sysname in (("breakout_s1a", "S1-A"), ("breakout_s2a", "S2-A")):
        for item in scan_data.get(key, []):
            if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
                found[sysname] = item
    hot_item = None
    for item in (scan_data.get("hot_pool") or {}).get("hot_breakout", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            hot_item = item
    if hot_item:
        found["HOT-S"] = hot_item
    evt_item = None
    for item in (scan_data.get("hot_pool") or {}).get("event_candidates", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            evt_item = item
    if evt_item:
        found["EVT-S"] = evt_item
    if not found:
        return {"ok": False, "reason": f"{symbol} 不在最新突破候选列表中"}

    if system:
        if system not in found:
            return {"ok": False, "reason": f"{symbol} 不在 {system} 候选列表中", "available": list(found)}
        entry_system = system
    elif "S2-A" in found:
        entry_system = "S2-A"  # 同股双信号默认慢速
    elif "S1-A" in found:
        entry_system = "S1-A"
    elif "HOT-S" in found:
        entry_system = "HOT-S"
    else:
        entry_system = "EVT-S"
    item = found[entry_system]

    close = float(item["close"])
    atr = float(item.get("atr_20", 0) or 0)
    stop = round(close - atr * ATR_STOP_MULT, 2) if atr > 0 else round(close * 0.95, 2)
    account = account or _default_account_for_system(entry_system)
    state = derive_state_safe(log)
    calc = calc_position(equity=equity, entry=close, stop=stop, account_type=account,
                         drawdown_state=state, atr=atr or None)
    if calc["股数"] <= 0:
        return {"ok": False, "reason": "仓位计算结果为 0 股（预算不足一手）", "calc": calc}

    trade = Trade(
        股票代码=symbol, 股票名称=str(item.get("name", "") or ""),
        账户类型=account, 风险簇=lookup_cluster(symbol) or "未指定",
        入场系统=entry_system,
        核心逻辑=f"{entry_system} 突破：收盘 {close:.2f} 突破 {item.get('period', '?')} 日通道高点 "
                f"{item.get('channel_high', 0):.2f}（+{item.get('breakout_pct', 0):.2f}%）",
        入场价=close, 止损价=stop, 风险率=calc["风险率"],
        股数=calc["股数"], 仓位金额=calc["仓位金额"], 是否系统内交易=True,
    )
    gate = _gate(trade, log, equity, force, state)
    if not gate["allowed"]:
        return {"ok": False, "reason": "高级违规拒绝", "system": entry_system, "calc": calc, **gate}
    if gate["note"]:
        trade.备注 = gate["note"]
    log.add(trade)
    card = generate_buy_card(trade, calc, scan_info=item)
    return {"ok": True, "交易编号": trade.交易编号, "system": entry_system,
            "calc": calc, "买入卡": str(card) if card else None, **gate}


def execute_add_position(
    symbol: str, price: float | None = None, force: bool = False,
    equity: float | None = None, trade_log: TradeLog | None = None, db_path=None,
) -> dict:
    """金字塔加仓（对应 cli add-position）：触发判定 → 闸门 → 落库 → 全链止损上移"""
    import pandas as pd

    from pipeline.database import load_daily_quotes
    from pipeline.indicators import calc_atr
    from review.pyramid import add_unit_shares, check_add_trigger, get_unit_chain

    log = trade_log or TradeLog()
    equity = equity or ACCOUNT_EQUITY
    symbol = symbol.zfill(6)[-6:]
    chain = get_unit_chain(log.list_all(open_only=True), symbol)
    if not chain:
        return {"ok": False, "reason": f"{symbol} 无未平仓单位链（需先建仓）"}
    root = chain[0]

    df = load_daily_quotes(symbol=symbol, db_path=db_path)
    if df.empty:
        return {"ok": False, "reason": f"market.db 无 {symbol} 行情，无法判定加仓触发"}
    df = df.sort_values("trade_date").reset_index(drop=True)
    latest_close = float(df.iloc[-1]["close"])
    atr_series = calc_atr(df)
    atr = float(atr_series.iloc[-1]) if not atr_series.empty and pd.notna(atr_series.iloc[-1]) else 0.0

    exec_price = price or latest_close
    trig = check_add_trigger(chain, exec_price, atr)
    if not trig["可加仓"]:
        return {"ok": False, "reason": trig["原因"], "trigger": trig}

    state = derive_state_safe(log)
    if state != "Normal":
        return {"ok": False, "reason": f"回撤状态 {state}（非 Normal）禁止加仓（V5.0 §5.5）", "trigger": trig}

    new_stop = trig["统一止损价"]
    shares = add_unit_shares(root, exec_price - new_stop)
    if shares <= 0:
        return {"ok": False, "reason": "建议股数不足一手", "trigger": trig}

    trade = Trade(
        股票代码=symbol, 股票名称=root.股票名称, 账户类型=root.账户类型,
        风险簇=root.风险簇, 入场系统=root.入场系统,
        核心逻辑=f"金字塔加仓（首仓 {root.交易编号}，触发价 {trig['触发价']:.2f}）",
        入场价=exec_price, 止损价=new_stop,
        风险率=round((exec_price - new_stop) * shares / equity * 100, 3),
        股数=shares, 仓位金额=round(exec_price * shares, 2),
        是否系统内交易=True, 关联单号=root.交易编号, 单位序号=trig["下一单位序号"],
        备注=f"加仓 N={atr:.2f}，统一止损 {new_stop:.2f}",
    )
    gate = _gate(trade, log, equity, force, state)
    if not gate["allowed"]:
        return {"ok": False, "reason": "高级违规拒绝", "trigger": trig, **gate}
    if gate["note"]:
        trade.备注 = (trade.备注 + " " + gate["note"]).strip()
    log.add(trade)

    raised = []
    for t in chain:
        if new_stop > t.止损价:
            note = (t.备注 + f" [加仓后止损上移 {t.止损价:.2f}→{new_stop:.2f}]").strip()
            log.update(t.交易编号, 止损价=new_stop, 备注=note)
            raised.append(t.交易编号)
    return {"ok": True, "交易编号": trade.交易编号, "单位序号": trade.单位序号,
            "股数": shares, "统一止损价": new_stop, "止损上移": raised, "trigger": trig, **gate}


def execute_sell(
    symbol: str, shares: int, price: float, trade_id: str | None = None,
    exit_date: str | None = None, reason: str = "", exit_time: str = "",
    equity: float | None = None, trade_log: TradeLog | None = None,
) -> dict:
    """卖出登记（对应 cli sell）：全平自动算 R；部分卖出拆单"""
    from review.metrics import calc_r_multiple

    log = trade_log or TradeLog()
    equity = equity or ACCOUNT_EQUITY
    symbol = symbol.zfill(6)[-6:]

    if trade_id:
        target = log.get(trade_id)
        if not target or target.is_closed:
            return {"ok": False, "reason": f"{trade_id} 不存在或已平仓"}
    else:
        candidates = [t for t in log.get_by_symbol(symbol) if not t.is_closed]
        if not candidates:
            return {"ok": False, "reason": f"{symbol} 当前无持仓中交易"}
        target = max(candidates, key=lambda t: (t.日期 or "", t.创建时间 or ""))

    exit_date = exit_date or datetime.now().strftime("%Y-%m-%d")
    if shares >= target.股数:
        log.update(target.交易编号, 实际退出价=price, 退出日期=exit_date,
                   退出时间=exit_time, 退出原因=reason)
        r = calc_r_multiple(target.入场价, price, target.止损价)
        if r is not None:
            log.update(target.交易编号, R倍数=round(r, 2))
        return {"ok": True, "mode": "全平", "交易编号": target.交易编号, "R倍数": r}

    remain = target.股数 - shares
    note = (target.备注 + f" [分批卖出 {shares}股 @ {price:.2f}，余 {remain}股]").strip()
    log.update(target.交易编号, 股数=remain,
               仓位金额=round(target.入场价 * remain, 2), 备注=note)
    child = Trade(
        日期=target.日期, 入场时间=target.入场时间,
        股票代码=target.股票代码, 股票名称=target.股票名称,
        账户类型=target.账户类型, 风险簇=target.风险簇, 入场系统=target.入场系统,
        核心逻辑=target.核心逻辑, 入场价=target.入场价, 止损价=target.止损价,
        风险率=round(target.per_share_risk * shares / equity * 100, 3),
        股数=shares, 仓位金额=round(target.入场价 * shares, 2),
        实际退出价=price, 退出日期=exit_date, 退出时间=exit_time,
        退出原因=reason or "分批止盈", 是否系统内交易=target.是否系统内交易,
        关联单号=target.交易编号, 单位序号=target.单位序号,
        备注=f"分批卖出（来源 {target.交易编号}）",
    )
    r = calc_r_multiple(child.入场价, price, child.止损价)
    if r is not None:
        child.R倍数 = round(r, 2)
    log.add(child)
    return {"ok": True, "mode": "部分卖出", "交易编号": target.交易编号,
            "子单": child.交易编号, "剩余股数": remain, "R倍数": r}


def execute_update_stop(trade_id: str, stop: float, trade_log: TradeLog | None = None) -> dict:
    """补设/更新止损价（对应 cli update --stop；移动止损建议的人工执行入口）"""
    log = trade_log or TradeLog()
    trade = log.get(trade_id)
    if not trade:
        return {"ok": False, "reason": f"未找到交易 {trade_id}"}
    if trade.is_closed:
        return {"ok": False, "reason": f"{trade_id} 已平仓，无需更新止损"}
    old = trade.止损价
    note = (trade.备注 + f" [止损调整 {old:.2f}→{stop:.2f}]").strip()
    log.update(trade_id, 止损价=stop, 备注=note)
    return {"ok": True, "交易编号": trade_id, "旧止损": old, "新止损": stop}


# ---------- 内部工具 ----------

def _load_latest_scan_json() -> Optional[dict]:
    """读取最新 market_scan JSON 文件"""
    if not MARKET_SCAN_OUTPUT_DIR.exists():
        return None
    files = sorted(MARKET_SCAN_OUTPUT_DIR.glob("market_scan_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("扫描 JSON 读取失败: %s", e)
        return None
