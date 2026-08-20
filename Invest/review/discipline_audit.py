"""
纪律自动审计 — 交易行为纪律规则扫描（纯函数可测）

规则（阈值统一取自 config）：
1. 追高接回（高）：同一代码，已平仓笔的退出日期 == 另一笔的入场日期，且后者入场价 > 前者实际退出价；
   两笔都有时间字段时要求 入场时间 > 退出时间，无时间字段则按日期+价格降级判定并在描述中注明
2. 闪电换仓（中）：某笔买入的 日期+入场时间 与当日另一笔（不同代码）平仓的退出时间
   间隔 < DISCIPLINE_SWITCH_MINUTES；任一笔缺时间则跳过
3. 禁买板块（高）：代码前缀命中 BANNED_BOARD_PREFIXES
4. 无止损（中）：止损价 <= 0
5. 非系统交易（低）：是否系统内交易=False
6. 禁买新股（高）：名称 N/C 字头新股次新（六条硬规则①，BANNED_NEW_STOCK_ENABLED）
7. 开盘追高（中）：入场时间早于 OPEN_CHASE_CUTOFF（六条硬规则③）

用法:
    python review/discipline_audit.py     # 审计全部 trades 并打印
"""
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    BANNED_BOARD_PREFIXES,
    BANNED_NEW_STOCK_ENABLED,
    DISCIPLINE_SWITCH_MINUTES,
    OPEN_CHASE_CUTOFF,
)
from review.trade_log import Trade, TradeLog

logger = logging.getLogger(__name__)

RULE_CHASE = "追高接回"
RULE_SWITCH = "闪电换仓"
RULE_BANNED = "禁买板块"
RULE_NO_STOP = "无止损"
RULE_OFF_SYSTEM = "非系统交易"
RULE_NEW_STOCK = "禁买新股"
RULE_OPEN_CHASE = "开盘追高"

# 汇总表固定顺序与严重程度
RULE_ORDER = [
    RULE_CHASE, RULE_SWITCH, RULE_BANNED, RULE_NO_STOP, RULE_OFF_SYSTEM,
    RULE_NEW_STOCK, RULE_OPEN_CHASE,
]
RULE_SEVERITY = {
    RULE_CHASE: "高",
    RULE_SWITCH: "中",
    RULE_BANNED: "高",
    RULE_NO_STOP: "中",
    RULE_OFF_SYSTEM: "低",
    RULE_NEW_STOCK: "高",
    RULE_OPEN_CHASE: "中",
}


@dataclass
class Finding:
    """一条纪律审计发现"""
    规则: str
    严重程度: str
    交易编号: str
    股票代码: str
    描述: str
    建议: str = ""


def _minutes_between(time_a: str, time_b: str) -> float | None:
    """两个 HH:MM:SS 时间之差（分钟，b − a）；解析失败返回 None"""
    try:
        a = datetime.strptime(time_a, "%H:%M:%S")
        b = datetime.strptime(time_b, "%H:%M:%S")
        return (b - a).total_seconds() / 60
    except (ValueError, TypeError):
        return None


def _check_chase_back(trades: list[Trade]) -> list[Finding]:
    """追高接回（高）：同代码当日先卖后买且买价 > 卖价"""
    findings = []
    closed = [t for t in trades if t.is_closed]
    for buy in trades:
        for sell in closed:
            if sell.交易编号 == buy.交易编号 or sell.股票代码 != buy.股票代码:
                continue
            if not sell.退出日期 or sell.退出日期 != buy.日期:
                continue
            if sell.实际退出价 is None or buy.入场价 <= sell.实际退出价:
                continue
            if sell.退出时间 and buy.入场时间:
                gap = _minutes_between(sell.退出时间, buy.入场时间)
                if gap is None or gap <= 0:
                    continue  # 买在卖前或同时，非接回
                note = ""
            else:
                note = "（无时间数据，按日期判定）"
            findings.append(Finding(
                规则=RULE_CHASE,
                严重程度="高",
                交易编号=buy.交易编号,
                股票代码=buy.股票代码,
                描述=(f"同日先卖后买且买价更高：{buy.日期} 卖出 @{sell.实际退出价}"
                      f"（{sell.交易编号}）→ 接回 @{buy.入场价}{note}"),
                建议="卖出后当日不追高接回；确有新逻辑须重写买入卡再过入场合规闸门",
            ))
    return findings


def _check_fast_switch(trades: list[Trade]) -> list[Finding]:
    """闪电换仓（中）：卖出后 DISCIPLINE_SWITCH_MINUTES 分钟内买入另一只"""
    findings = []
    closed = [t for t in trades if t.is_closed]
    for buy in trades:
        if not buy.入场时间:
            continue
        for sell in closed:
            if sell.交易编号 == buy.交易编号 or sell.股票代码 == buy.股票代码:
                continue
            if not sell.退出日期 or sell.退出日期 != buy.日期 or not sell.退出时间:
                continue
            gap = _minutes_between(sell.退出时间, buy.入场时间)
            if gap is None or not (0 <= gap < DISCIPLINE_SWITCH_MINUTES):
                continue
            findings.append(Finding(
                规则=RULE_SWITCH,
                严重程度="中",
                交易编号=buy.交易编号,
                股票代码=buy.股票代码,
                描述=(f"卖出 {sell.股票代码}（{sell.交易编号} {sell.退出时间}）后 "
                      f"{gap:.0f} 分钟内买入（{buy.入场时间}），间隔 < {DISCIPLINE_SWITCH_MINUTES} 分钟"),
                建议=f"换仓间隔过短多为冲动操作；卖出后至少间隔 {DISCIPLINE_SWITCH_MINUTES} 分钟再评估下一笔",
            ))
    return findings


def _check_banned_board(trades: list[Trade]) -> list[Finding]:
    """禁买板块（高）：代码前缀命中 BANNED_BOARD_PREFIXES"""
    findings = []
    for t in trades:
        symbol = t.股票代码.zfill(6)[-6:] if t.股票代码 else ""
        if not symbol or not any(symbol.startswith(p) for p in BANNED_BOARD_PREFIXES):
            continue
        findings.append(Finding(
            规则=RULE_BANNED,
            严重程度="高",
            交易编号=t.交易编号,
            股票代码=t.股票代码,
            描述=f"代码 {symbol} 命中禁买板块前缀 {'/'.join(BANNED_BOARD_PREFIXES)}（BANNED_BOARD_PREFIXES 配置）",
            建议="移出股票池；复盘当时为何破例",
        ))
    return findings


def _check_no_stop(trades: list[Trade]) -> list[Finding]:
    """无止损（中）：止损价 <= 0"""
    return [
        Finding(
            规则=RULE_NO_STOP,
            严重程度="中",
            交易编号=t.交易编号,
            股票代码=t.股票代码,
            描述="未设置止损价",
            建议="所有交易必须在入场前定义止损价",
        )
        for t in trades if t.止损价 <= 0
    ]


def _check_off_system(trades: list[Trade]) -> list[Finding]:
    """非系统交易（低）：是否系统内交易=False"""
    return [
        Finding(
            规则=RULE_OFF_SYSTEM,
            严重程度="低",
            交易编号=t.交易编号,
            股票代码=t.股票代码,
            描述="该交易标记为非系统内交易",
            建议="复盘原因，避免重复",
        )
        for t in trades if not t.是否系统内交易
    ]


def _check_new_stock(trades: list[Trade]) -> list[Finding]:
    """禁买新股（高）：名称 N/C 字头新股次新（六条硬规则①）"""
    findings = []
    for t in trades:
        name = (t.股票名称 or "").strip()
        if not name or name.upper()[:1] not in ("N", "C"):
            continue
        findings.append(Finding(
            规则=RULE_NEW_STOCK,
            严重程度="高",
            交易编号=t.交易编号,
            股票代码=t.股票代码,
            描述=f"「{t.股票名称}」为 N/C 字头新股次新（六条硬规则：永久拉黑）",
            建议="新股次新永久拉黑，不再以任何理由买入",
        ))
    return findings


def _check_open_chase(trades: list[Trade]) -> list[Finding]:
    """开盘追高（中）：入场时间早于 OPEN_CHASE_CUTOFF（六条硬规则③）"""
    findings = []
    for t in trades:
        entry_time = (t.入场时间 or "").strip()
        if not entry_time or entry_time[:5] >= OPEN_CHASE_CUTOFF:
            continue
        findings.append(Finding(
            规则=RULE_OPEN_CHASE,
            严重程度="中",
            交易编号=t.交易编号,
            股票代码=t.股票代码,
            描述=f"入场时间 {entry_time} 早于 {OPEN_CHASE_CUTOFF}，属开盘追高（六条硬规则③）",
            建议="改为尾盘 14:30 后或盘中回调时买入；开盘瞬间冲进去的大亏单最多",
        ))
    return findings


def audit_discipline(trades: list[Trade]) -> list[Finding]:
    """对交易列表跑全部纪律规则，返回 Finding 列表（按规则固定顺序分组）"""
    findings: list[Finding] = []
    findings.extend(_check_chase_back(trades))
    findings.extend(_check_fast_switch(trades))
    findings.extend(_check_banned_board(trades))
    findings.extend(_check_no_stop(trades))
    findings.extend(_check_off_system(trades))
    if BANNED_NEW_STOCK_ENABLED:
        findings.extend(_check_new_stock(trades))
    findings.extend(_check_open_chase(trades))
    return findings


def audit_to_markdown(findings: list[Finding]) -> str:
    """审计结果 → Markdown：先汇总表（各规则命中数），再明细表；空则 [PASS]"""
    if not findings:
        return "[PASS] 未发现纪律问题"

    counts = {rule: 0 for rule in RULE_ORDER}
    for f in findings:
        counts[f.规则] = counts.get(f.规则, 0) + 1

    lines = [
        "### 纪律审计汇总",
        "",
        "| 规则 | 严重程度 | 命中数 |",
        "| --- | --- | --- |",
    ]
    for rule in RULE_ORDER:
        lines.append(f"| {rule} | {RULE_SEVERITY[rule]} | {counts[rule]} |")
    lines += [
        "",
        "### 纪律审计明细",
        "",
        "| 规则 | 严重程度 | 交易编号 | 代码 | 描述 | 建议 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for f in findings:
        lines.append(f"| {f.规则} | {f.严重程度} | {f.交易编号} | {f.股票代码} | {f.描述} | {f.建议} |")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    trades = TradeLog().list_all()
    print(f"[OK] 纪律审计：共 {len(trades)} 笔交易")
    print(audit_to_markdown(audit_discipline(trades)))


if __name__ == "__main__":
    main()
