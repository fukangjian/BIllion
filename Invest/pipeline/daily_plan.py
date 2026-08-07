"""
盘前操作清单（明日操作计划）— V5.0 盘前步骤 4-6 的自动化产出

把扫描候选与持仓监控汇总成可直接执行的清单：
- 买入候选：滤网全过（record=True）的突破信号，附参考买入价/建议止损/建议股数/风险率
  （calc_position 口径）与闸门预检结论（check_entry，含禁买板块/总热度/市场状态门禁）；
- 持仓行动：止损/退出/接近止损/无止损/移动止损建议逐条转行动；
- 不交易条件：按市场状态与体系冷却规则生成。

工具只整理与计算，决策与下单由人执行（V5.0 §11：量化告警而非自动重仓）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
from typing import Optional

from config import (
    ACCOUNT_EQUITY,
    ATR_STOP_MULT,
    DAILY_PLAN_MAX_CANDIDATES,
    SYSTEM_DEFAULT_ACCOUNT,
)
from position_calculator import calc_position

logger = logging.getLogger(__name__)


def _name_lookup(db_path=None) -> dict[str, str]:
    """代码 → 名称映射（离线：趋势池 + 热点池当日表；查不到为空串）"""
    names: dict[str, str] = {}
    try:
        from pipeline.database import load_hot_pool, load_trend_pool

        for loader in (load_trend_pool, load_hot_pool):
            df = loader(db_path=db_path)
            if df is not None and not df.empty and "name" in df.columns:
                for _, r in df.iterrows():
                    sym = str(r["symbol"]).zfill(6)[-6:]
                    if r.get("name") and sym not in names:
                        names[sym] = str(r["name"])
    except Exception as e:
        logger.debug("名称映射读取失败（降级为空）: %s", e)
    return names


def _gate_precheck(symbol, system, account, close, stop, calc, equity, state, trade_log, db_path) -> str:
    """建仓闸门预检：返回 通过 / ⚠️警告 / ⛔拒绝原因；预检本身失败降级标注"""
    try:
        from review.entry_gate import check_entry, split_by_severity
        from review.trade_log import Trade

        trade = Trade(
            股票代码=symbol, 账户类型=account, 入场系统=system,
            入场价=close, 止损价=stop,
            风险率=calc["风险率"], 股数=calc["股数"], 仓位金额=calc["仓位金额"],
        )
        violations, _ = check_entry(
            trade, trade_log=trade_log, account_equity=equity,
            drawdown_state=state, db_path=db_path,
        )
        high, others = split_by_severity(violations)
        if high:
            return "⛔ " + "、".join(v.违规类型 for v in high)
        if others:
            return "⚠️ " + "、".join(v.违规类型 for v in others)
        return "通过"
    except Exception as e:
        logger.warning("闸门预检失败 %s（降级标注）: %s", symbol, e)
        return "预检失败"


def build_daily_plan(
    scan_json: dict,
    monitor_result: Optional[dict],
    equity: float = None,
    trade_log=None,
    db_path=None,
) -> dict:
    """
    汇总扫描 JSON 与持仓监控结果为明日操作计划（失败逐段降级，不抛异常）。

    返回 {date, market_state, drawdown_state, buy_candidates, position_actions, no_trade_conditions}
    """
    equity = equity or ACCOUNT_EQUITY
    market_state = scan_json.get("market_state", "")
    dd = (monitor_result or {}).get("drawdown_state", {})
    state = dd.get("state") or "Normal"
    names = _name_lookup(db_path)

    # ---- 买入候选：滤网全过且未冷却的突破信号，按突破幅度降序取前 N ----
    pool: list[tuple[str, dict]] = []
    for key, system in (("breakout_s1a", "S1-A"), ("breakout_s2a", "S2-A")):
        for item in scan_json.get(key, []):
            if not item.get("record", True):
                continue
            fp, fr = item.get("filter_passed"), item.get("filters_required")
            if fp is None or fr is None or fr == 0 or fp != fr:
                continue
            pool.append((system, item))
    pool.sort(key=lambda x: -(x[1].get("breakout_pct") or 0))
    # 同股双系统信号去重：保留 S2-A（慢速更稳，与 from-scan 默认选择一致）
    seen: dict[str, tuple[str, dict]] = {}
    for system, item in pool:
        sym = str(item["symbol"]).zfill(6)[-6:]
        if sym not in seen or "S2" in system:
            seen[sym] = (system, item)
    pool = sorted(seen.values(), key=lambda x: -(x[1].get("breakout_pct") or 0))
    pool = pool[:DAILY_PLAN_MAX_CANDIDATES]

    trend_buys: list[dict] = []
    for system, item in pool:
        symbol = str(item["symbol"]).zfill(6)[-6:]
        close = float(item["close"])
        atr = float(item.get("atr_20") or 0)
        stop = round(close - atr * ATR_STOP_MULT, 2) if atr > 0 else round(close * 0.95, 2)
        account = SYSTEM_DEFAULT_ACCOUNT.get(system.split("-")[0], "产业")
        calc = calc_position(
            equity=equity, entry=close, stop=stop, account_type=account,
            drawdown_state=state, atr=atr or None,
        )
        gate = _gate_precheck(symbol, system, account, close, stop, calc,
                              equity, state, trade_log, db_path)
        note_parts = [p for p in (item.get("note"), *calc.get("备注", [])) if p]
        trend_buys.append({
            "symbol": symbol,
            "name": names.get(symbol, ""),
            "system": system,
            "close": round(close, 2),
            "stop": stop,
            "shares": calc["股数"],
            "risk_pct": calc["风险率"],
            "position_pct": round(calc.get("仓位比例", 0), 1),
            "add_price1": calc.get("加仓价1"),
            "account": account,
            "gate": gate,
            "filter_brief": item.get("filter_brief", ""),
            "note": "；".join(note_parts),
        })

    # ---- 龙头候选（主，超短 HOT-S）：热点池三维验证评分 S/A/B 级 ----
    dragon_buys: list[dict] = []
    for rec in (scan_json.get("hot_pool") or {}).get("dragon_candidates", []):
        symbol = str(rec.get("symbol", "")).zfill(6)[-6:]
        if not symbol or not rec.get("close"):
            continue
        close = float(rec["close"])
        atr = float(rec.get("atr_20") or 0)
        stop = round(close - atr * ATR_STOP_MULT, 2) if atr > 0 else round(close * 0.95, 2)
        account = "事件"  # HOT-S 超短热点默认事件账户（与 cli from-scan 本地特判一致）
        calc = calc_position(
            equity=equity, entry=close, stop=stop, account_type=account,
            drawdown_state=state, atr=atr or None,
        )
        gate = _gate_precheck(symbol, "HOT-S", account, close, stop, calc,
                              equity, state, trade_log, db_path)
        note_parts = [p for p in (rec.get("analysis"), *calc.get("备注", [])) if p]
        dragon_buys.append({
            "symbol": symbol,
            "name": str(rec.get("name", "") or ""),
            "system": "HOT-S",
            "grade": rec.get("dragon_grade", ""),
            "score": rec.get("dragon_score", 0),
            "dims": rec.get("dragon_dims", {}),
            "lbc": rec.get("lbc", 0),
            "sector": rec.get("sector", ""),
            "close": round(close, 2),
            "stop": stop,
            "shares": calc["股数"],
            "risk_pct": calc["风险率"],
            "position_pct": round(calc.get("仓位比例", 0), 1),
            "account": account,
            "gate": gate,
            "note": "；".join(note_parts),
        })

    # ---- 持仓行动：监控警报逐条转行动 ----
    position_actions: list[dict] = []
    for a in (monitor_result or {}).get("alerts", []):
        position_actions.append({
            "交易编号": a.get("交易编号", ""),
            "股票代码": a.get("股票代码", ""),
            "股票名称": a.get("股票名称", ""),
            "类型": a.get("类型", ""),
            "现价": a.get("现价"),
            "行动": a.get("建议动作", ""),
        })

    # ---- 不交易条件（市场状态 + 体系冷却规则） ----
    no_trade: list[str] = []
    if market_state == "D":
        no_trade.append("市场状态 D（系统性下跌）：禁止新开趋势仓（闸门高级违规），只保留核心仓和极小验证仓")
    elif market_state == "C":
        no_trade.append("市场状态 C（震荡轮动）：只做滤网全过的 A 级信号，趋势仓建议风险减半")
    no_trade.extend([
        "临时发现的热点：先写下来源/催化/失效条件/止损/仓位，等 30 分钟再决定（30 分钟规则）",
        "连续 2 笔 −1R 后下一笔风险减半；连续 3 笔亏损暂停两天复盘（V5.0 §6.3）",
        "重大公告后计划建 >5% 仓位或「必须马上买」的感觉出现时：72 小时内只小仓试错（72 小时规则）",
    ])

    return {
        "date": scan_json.get("date", ""),
        "market_state": market_state,
        "drawdown_state": state,
        "dragon_buys": dragon_buys,
        "trend_buys": trend_buys,
        "position_actions": position_actions,
        "no_trade_conditions": no_trade,
    }


def daily_plan_to_markdown(plan: dict) -> list[str]:
    """明日操作计划 → Markdown 行（嵌入扫描报告「一、市场状态判断」之后）"""
    lines = [
        "## 二、明日操作计划",
        "",
        f"> 口径：滤网全过的突破候选 ≤{DAILY_PLAN_MAX_CANDIDATES} 只，止损/股数按 calc_position 自动算好；"
        "清单是候选与参数，最终决策与下单由人执行（V5.0 §11）。",
        "",
    ]

    dragons = plan.get("dragon_buys", [])
    trends = plan.get("trend_buys", [])
    total = len(dragons) + len(trends)
    lines.append(f"### 买入候选（{total}）")
    lines.append("")
    if not total:
        lines.append("_明日无滤网全过的突破候选、无达标龙头——不交易也是操作。_")
    else:
        if dragons:
            lines.extend([
                f"#### 🐉 龙头候选（超短 HOT-S，三维验证 S/A/B 级，{len(dragons)} 只）",
                "",
                "| 代码 | 名称 | 等级 | 总分 | 板块·连板 | 参考买入价 | 建议止损 | 建议股数 | 风险率% | 闸门预检 |",
                "|------|------|------|------|-----------|-----------|----------|----------|---------|----------|",
            ])
            for b in dragons:
                lines.append(
                    f"| {b['symbol']} | {b.get('name') or '-'} | **{b['grade']}** | {b['score']} "
                    f"| {b.get('sector') or '-'}·{b.get('lbc', 0)}板 | {b['close']:.2f} "
                    f"| {b['stop']:.2f} | {b['shares']} | {b['risk_pct']} | {b['gate']} |"
                )
            lines.append("")
        if trends:
            lines.extend([
                f"#### 📈 趋势候选（S1-A/S2-A 滤网全过，{len(trends)} 只）",
                "",
                "| 代码 | 名称 | 系统 | 参考买入价 | 建议止损 | 建议股数 | 风险率% | 仓位% | 闸门预检 | 备注 |",
                "|------|------|------|-----------|----------|----------|---------|-------|----------|------|",
            ])
            for b in trends:
                lines.append(
                    f"| {b['symbol']} | {b.get('name') or '-'} | {b['system']} | {b['close']:.2f} "
                    f"| {b['stop']:.2f} | {b['shares']} | {b['risk_pct']} | {b['position_pct']} "
                    f"| {b['gate']} | {b.get('note') or '—'} |"
                )
            lines.append("")
        lines.append("> 执行入口：`python review/cli.py from-scan <代码> --execute`（一键建仓："
                     "仓位计算 → 合规闸门 → 写库 → 买入卡）；浮盈 0.5N 后用 `add-position <代码>` 加仓。"
                     "龙头候选为超短 HOT-S（5 日强制结算），仓位上限与止损按事件账户口径。")
    lines.append("")

    actions = plan.get("position_actions", [])
    lines.append(f"### 持仓行动（{len(actions)}）")
    lines.append("")
    if not actions:
        lines.append("_持仓无警报，按原计划持有。_")
    else:
        lines.append("| 编号 | 代码 | 名称 | 类型 | 现价 | 行动 |")
        lines.append("|------|------|------|------|------|------|")
        for a in actions:
            price = f"{a['现价']:.2f}" if a.get("现价") is not None else "-"
            lines.append(
                f"| {a['交易编号']} | {a['股票代码']} | {a.get('股票名称') or '-'} "
                f"| **{a['类型']}** | {price} | {a['行动']} |"
            )
    lines.append("")

    no_trade = plan.get("no_trade_conditions", [])
    if no_trade:
        lines.append("### 不交易条件")
        lines.append("")
        for c in no_trade:
            lines.append(f"- {c}")
        lines.append("")
    return lines
