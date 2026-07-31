"""
持仓监控 — 止损/退出通道检查、浮动盈亏 R、回撤状态自动推导

盘前流程闭环：建仓后每日检查未平仓持仓是否触发止损或系统退出信号。
行情数据全部来自本地 market.db（离线），不联网。

用法:
    python review/monitor.py        # 打印当日监控结果并写 position_monitor_{date}.json
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import logging
import os
from datetime import datetime
from typing import Optional

import pandas as pd

from config import (
    ATR_PERIOD,
    DRAWDOWN_THRESHOLDS,
    EXIT_CHANNEL_PERIODS,
    MARKET_SCAN_OUTPUT_DIR,
    MONTHLY_DRAWDOWN_LIMITS,
)
from pipeline.database import load_daily_quotes
from pipeline.indicators import calc_atr, calc_donchian_channel
from review.metrics import calc_r_multiple
from review.trade_log import Trade, TradeLog

logger = logging.getLogger(__name__)

# 回撤状态严重度排序（数值越大越严重）
_STATE_SEVERITY = {"Normal": 0, "Caution": 1, "Defensive": 2, "Review": 3}

# 警报优先级：止损 > 退出 > 接近止损
_ALERT_PRIORITY = {"止损": 0, "退出": 1, "接近止损": 2}


def _normalize_symbol(symbol: str) -> str:
    """标准化股票代码为 6 位"""
    return str(symbol).zfill(6)[-6:]


def _exit_channel_period(entry_system: str) -> Optional[int]:
    """按入场系统匹配退出通道周期：含 S1→10 日，含 S2→20 日，其它系统→None（只查止损）"""
    s = (entry_system or "").upper()
    for key, period in EXIT_CHANNEL_PERIODS.items():
        if key in s:
            return period
    return None


def _check_single_position(trade: Trade, db_path: Optional[Path] = None) -> dict:
    """检查单个未平仓持仓，返回结构化结果（"类型" 非空即为警报）"""
    sym = _normalize_symbol(trade.股票代码)
    info: dict = {
        "交易编号": trade.交易编号,
        "股票代码": sym,
        "股票名称": trade.股票名称,
        "类型": None,
        "现价": None,
        "止损价": trade.止损价,
        "通道下轨": None,
        "浮动R": None,
        "距止损N": None,
        "数据日期": None,
        "建议动作": "",
        "备注": "",
    }

    df = load_daily_quotes(symbol=sym, db_path=db_path)
    if df.empty:
        info["备注"] = "market.db 无该股行情，未检查"
        return info

    df = df.sort_values("trade_date").reset_index(drop=True)
    latest = df.iloc[-1]
    close = float(latest["close"])
    info["现价"] = close
    info["数据日期"] = str(latest["trade_date"])

    # 浮动 R = (现价 − 入场价) / (入场价 − 止损价)
    risk_per_share = trade.入场价 - trade.止损价
    if risk_per_share > 0:
        info["浮动R"] = round((close - trade.入场价) / risk_per_share, 2)

    # 距止损距离（ATR 倍数，< 1N 提示接近止损）
    atr_series = calc_atr(df, ATR_PERIOD)
    atr = None
    if not atr_series.empty and pd.notna(atr_series.iloc[-1]):
        atr = float(atr_series.iloc[-1])
    if atr and atr > 0 and trade.止损价 > 0:
        info["距止损N"] = round((close - trade.止损价) / atr, 2)

    # 退出通道（shift(1)，不含当根 K 线，与回测/扫描口径一致，无未来函数）
    period = _exit_channel_period(trade.入场系统)
    channel_low = None
    if period is not None and len(df) >= period + 1:
        ch = calc_donchian_channel(df, period)
        val = ch[f"low_{period}"].iloc[-1]
        if pd.notna(val):
            channel_low = float(val)
            info["通道下轨"] = channel_low

    # 警报判定（单持仓只报最高优先级一条）
    if trade.止损价 > 0 and close <= trade.止损价:
        info["类型"] = "止损"
        info["建议动作"] = "收盘价已跌破止损价，按纪律立即退出"
    elif channel_low is not None and close < channel_low:
        info["类型"] = "退出"
        info["建议动作"] = f"收盘价跌破 {period} 日通道下轨，按系统退出"
    elif info["距止损N"] is not None and info["距止损N"] < 1:
        info["类型"] = "接近止损"
        info["建议动作"] = "距止损不足 1N，关注风险，勿加仓"

    return info


def check_positions(trade_log: TradeLog, db_path: Optional[Path] = None) -> dict:
    """遍历全部未平仓持仓，检查止损/退出通道/距止损距离，并推导回撤状态"""
    open_trades = trade_log.list_all(open_only=True)
    alerts: list[dict] = []
    positions_ok: list[dict] = []
    data_dates: list[str] = []
    floating_r_total = 0.0

    for t in open_trades:
        info = _check_single_position(t, db_path=db_path)
        if info["数据日期"]:
            data_dates.append(info["数据日期"])
        if info["浮动R"] is not None:
            floating_r_total += info["浮动R"]
        if info["类型"]:
            alerts.append(info)
        else:
            positions_ok.append(info)

    alerts.sort(key=lambda a: _ALERT_PRIORITY.get(a["类型"], 99))

    drawdown = derive_drawdown_state(trade_log, db_path=db_path, floating_r=floating_r_total)

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "data_date": max(data_dates) if data_dates else None,
        "open_count": len(open_trades),
        "alerts": alerts,
        "positions_ok": positions_ok,
        "drawdown_state": drawdown,
    }


def _calc_total_floating_r(open_trades: list[Trade], db_path: Optional[Path] = None) -> tuple[float, int]:
    """汇总未平仓浮动 R（以 market.db 最新收盘价为市价），返回 (合计, 缺行情/无法计算只数)"""
    total = 0.0
    missing = 0
    for t in open_trades:
        risk_per_share = t.入场价 - t.止损价
        if risk_per_share <= 0:
            missing += 1
            continue
        df = load_daily_quotes(symbol=_normalize_symbol(t.股票代码), db_path=db_path)
        if df.empty:
            missing += 1
            continue
        close = float(df.sort_values("trade_date").iloc[-1]["close"])
        total += (close - t.入场价) / risk_per_share
    return total, missing


def derive_drawdown_state(
    trade_log: TradeLog,
    db_path: Optional[Path] = None,
    floating_r: Optional[float] = None,
) -> dict:
    """
    回撤状态自动推导（投资体系 V5.0 §6）。

    近似口径：以「累计 R × 平均风险率」近似权益百分比回撤，
    忽略单笔仓位金额差异与复利效应；R 曲线 = 已平仓累计 R（按退出日期排序）+ 当前未平仓浮动 R。

    环境变量 DRAWDOWN_STATE 显式设置且与推导值冲突时，输出标注两者（以推导值为准，提示人工确认）。
    """
    trades = trade_log.list_all()
    open_trades = [t for t in trades if not t.is_closed]
    closed = sorted(
        [t for t in trades if t.is_closed],
        key=lambda t: t.退出日期 or "",
    )

    risk_rates = [t.风险率 for t in trades if t.风险率 > 0]
    avg_risk = sum(risk_rates) / len(risk_rates) if risk_rates else 0.0

    notes: list[str] = ["近似口径：累计R × 平均风险率"]

    # 已平仓累计 R 曲线（优先取存储的 R倍数，否则按入场/退出/止损重算）
    cum_r = 0.0
    peak_r = 0.0
    closed_r: list[tuple[Trade, float]] = []
    for t in closed:
        r = t.R倍数 if t.R倍数 is not None else calc_r_multiple(t.入场价, t.实际退出价, t.止损价)
        if r is None:
            continue
        closed_r.append((t, r))
        cum_r += r
        peak_r = max(peak_r, cum_r)

    # 未平仓浮动 R
    missing = 0
    if floating_r is None:
        floating_r, missing = _calc_total_floating_r(open_trades, db_path)
    if missing:
        notes.append(f"{missing} 只持仓缺行情或止损无效，浮动R未计入")

    current_r = cum_r + floating_r
    peak_r = max(peak_r, current_r)
    drawdown_pct = (peak_r - current_r) * avg_risk

    # 阈值映射（按严重度升序遍历，满足的最高档生效）
    state = "Normal"
    for name in ("Caution", "Defensive", "Review"):
        if drawdown_pct >= DRAWDOWN_THRESHOLDS[name]:
            state = name

    # 月度轨道：当月已实现 R × 平均风险率
    month_prefix = datetime.now().strftime("%Y-%m")
    monthly_r = sum(r for t, r in closed_r if (t.退出日期 or "").startswith(month_prefix))
    monthly_pct = monthly_r * avg_risk
    monthly_flags = {
        "event_trade_halt": monthly_pct <= MONTHLY_DRAWDOWN_LIMITS["event_trade_halt"],
        "new_position_halt": monthly_pct <= MONTHLY_DRAWDOWN_LIMITS["new_position_halt"],
    }

    # 环境变量显式设置时与推导值比对（os.getenv 而非 config，以区分"显式设置"与默认值）
    env_state = os.getenv("DRAWDOWN_STATE")
    conflict = env_state is not None and env_state in _STATE_SEVERITY and env_state != state
    if conflict:
        notes.append(f"环境变量 DRAWDOWN_STATE={env_state} 与推导值 {state} 冲突，以推导值为准，请人工确认")

    if not trades:
        state = "Normal"
        notes.append("样本为空，默认 Normal")

    return {
        "state": state,                     # 推导值（以此为准）
        "derived": state,
        "env": env_state,                   # None 表示未显式设置
        "conflict": conflict,
        "drawdown_pct": round(drawdown_pct, 2),
        "peak_r": round(peak_r, 2),
        "current_r": round(current_r, 2),
        "floating_r": round(floating_r, 2),
        "avg_risk_rate": round(avg_risk, 3),
        "monthly_realized_r": round(monthly_r, 2),
        "monthly_realized_pct": round(monthly_pct, 2),
        "monthly_flags": monthly_flags,
        "note": "；".join(notes),
    }


def run_monitor(
    trade_log: Optional[TradeLog] = None,
    db_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
) -> dict:
    """执行持仓监控并写 position_monitor_{date}.json（机器消费），返回监控结果"""
    log = trade_log or TradeLog()
    result = check_positions(log, db_path=db_path)

    out = output_dir or MARKET_SCAN_OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"position_monitor_{result['date']}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("持仓监控 JSON 已生成: %s", json_path)
    return result


def _fmt(v: Optional[float]) -> str:
    return f"{v:.2f}" if v is not None else "-"


def _fmt_r(v: Optional[float]) -> str:
    return f"{v:+.2f}" if v is not None else "-"


def monitor_to_markdown(result: dict) -> list[str]:
    """监控结果转 Markdown 行（供市场扫描报告顶部嵌入）"""
    dd = result.get("drawdown_state", {})
    data_date = result.get("data_date") or "无数据"
    state = dd.get("state", "Normal")
    conflict_note = ""
    if dd.get("conflict"):
        conflict_note = f"（⚠️ 与环境变量 {dd.get('env')} 冲突，推导值为准）"

    lines = [
        "## 持仓监控",
        "",
        f"> 行情数据日期: {data_date} | 回撤状态: **{state}**{conflict_note} "
        f"| 推导回撤: {dd.get('drawdown_pct', 0)}% | 当月已实现R: {dd.get('monthly_realized_r', 0)}",
        "",
    ]

    alerts = result.get("alerts", [])
    positions_ok = result.get("positions_ok", [])

    if not alerts and not positions_ok:
        lines.append("_当前无持仓，无需监控_")
    if alerts:
        lines.extend([
            f"### ⚠️ 警报（{len(alerts)}）",
            "",
            "| 编号 | 代码 | 名称 | 类型 | 现价 | 止损价 | 通道下轨 | 浮动R | 建议动作 |",
            "|------|------|------|------|------|--------|----------|-------|----------|",
        ])
        for a in alerts:
            lines.append(
                f"| {a['交易编号']} | {a['股票代码']} | {a['股票名称']} "
                f"| **⚠️ {a['类型']}** | {_fmt(a['现价'])} | {_fmt(a['止损价'])} "
                f"| {_fmt(a['通道下轨'])} | {_fmt_r(a['浮动R'])} | {a['建议动作']} |"
            )
        lines.append("")
    if positions_ok:
        lines.extend([
            f"### 正常持仓（{len(positions_ok)}）",
            "",
            "| 编号 | 代码 | 名称 | 现价 | 止损价 | 通道下轨 | 浮动R | 距止损N | 备注 |",
            "|------|------|------|------|--------|----------|-------|---------|------|",
        ])
        for p in positions_ok:
            lines.append(
                f"| {p['交易编号']} | {p['股票代码']} | {p['股票名称']} "
                f"| {_fmt(p['现价'])} | {_fmt(p['止损价'])} | {_fmt(p['通道下轨'])} "
                f"| {_fmt_r(p['浮动R'])} | {_fmt(p['距止损N'])} | {p['备注']} |"
            )
        lines.append("")

    flags = dd.get("monthly_flags", {})
    if flags.get("event_trade_halt"):
        lines.append("**⚠️ 月度回撤触线：停止事件交易**")
    if flags.get("new_position_halt"):
        lines.append("**⚠️ 月度回撤触线：停止开新仓**")
    return lines


def format_monitor_text(result: dict) -> str:
    """监控结果转纯文本（CLI 打印用）"""
    dd = result.get("drawdown_state", {})
    lines = [
        f"===== 持仓监控 {result.get('date')} =====",
        f"行情数据日期: {result.get('data_date') or '无数据'}",
        f"回撤状态: {dd.get('state')}（推导回撤 {dd.get('drawdown_pct')}%，"
        f"当月已实现R {dd.get('monthly_realized_r')}）",
    ]
    if dd.get("conflict"):
        lines.append(f"⚠️ 环境变量 DRAWDOWN_STATE={dd.get('env')} 与推导值冲突，推导值为准，请人工确认")

    alerts = result.get("alerts", [])
    positions_ok = result.get("positions_ok", [])
    if not alerts and not positions_ok:
        lines.append("当前无持仓，无需监控")
    for a in alerts:
        lines.append(
            f"⚠️ [{a['类型']}] {a['交易编号']} {a['股票代码']} {a['股票名称']} "
            f"现价 {_fmt(a['现价'])} 止损 {_fmt(a['止损价'])} → {a['建议动作']}"
        )
    if positions_ok:
        lines.append(f"正常持仓 ({len(positions_ok)}):")
        for p in positions_ok:
            extra = f" 备注: {p['备注']}" if p["备注"] else ""
            lines.append(
                f"  {p['股票代码']} {p['股票名称']} 现价 {_fmt(p['现价'])} "
                f"止损 {_fmt(p['止损价'])} 通道下轨 {_fmt(p['通道下轨'])} "
                f"浮动R {_fmt_r(p['浮动R'])} 距止损 {_fmt(p['距止损N'])}N{extra}"
            )

    flags = dd.get("monthly_flags", {})
    if flags.get("event_trade_halt"):
        lines.append("⚠️ 月度回撤触线：停止事件交易")
    if flags.get("new_position_halt"):
        lines.append("⚠️ 月度回撤触线：停止开新仓")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_monitor()
    print(format_monitor_text(result))
    json_path = MARKET_SCAN_OUTPUT_DIR / f"position_monitor_{result['date']}.json"
    print(f"\n监控 JSON: {json_path}")


if __name__ == "__main__":
    main()
