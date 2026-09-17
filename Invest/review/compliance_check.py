"""
规则偏差检查 — 基于投资体系 V5.0 的合规审计
"""
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from config import (
    ACCOUNT_EQUITY,
    BANNED_BOARD_PREFIXES,
    BANNED_NEW_STOCK_ENABLED,
    CONSECUTIVE_LOSS_HALT_COUNT,
    CONSECUTIVE_LOSS_HALT_DAYS,
    DRAWDOWN_STATE,
    FORBIDDEN_IN_DRAWDOWN,
    MAX_OPEN_POSITIONS,
    MAX_SINGLE_POSITION_PCT,
    MAX_SINGLE_RISK_PCT,
    MAX_WEEKLY_ENTRIES,
    OPEN_CHASE_CUTOFF,
    PORTFOLIO_HEAT_LIMITS,
    POSITION_LIMITS,
    RISK_CLUSTER_LIMITS,
    RISK_LIMITS_DRAWDOWN,
    RISK_LIMITS_NORMAL,
    SENTIMENT_BAN_PHASES,
    STOP_SUGGEST_MAX_PCT,
    STRATEGY_CODES,
)
from review.trade_log import Trade, TradeLog

logger = logging.getLogger(__name__)


@dataclass
class Violation:
    交易编号: str
    股票代码: str
    违规类型: str
    严重程度: str
    描述: str
    建议: str = ""


@dataclass
class ComplianceReport:
    检查交易数: int = 0
    违规笔数: int = 0
    违规列表: list[Violation] = field(default_factory=list)
    回撤状态: str = DRAWDOWN_STATE

    @property
    def 合规率(self) -> float:
        if self.检查交易数 == 0:
            return 100.0
        clean = self.检查交易数 - len({v.交易编号 for v in self.违规列表})
        return round(clean / self.检查交易数 * 100, 1)

    def add(self, violation: Violation):
        self.违规列表.append(violation)


def get_risk_limit(account_type: str, drawdown_state: str) -> float:
    """账户类型在给定回撤状态下的单笔风险率上限（%），供合规检查与仓位计算共用"""
    if drawdown_state in ("Caution", "Defensive", "Review"):
        for key, limit in RISK_LIMITS_DRAWDOWN.items():
            if key in account_type or account_type in key:
                return limit
        return 0.25
    for key, limit in RISK_LIMITS_NORMAL.items():
        if key in account_type or account_type in key:
            return limit
    return 0.5


def get_position_limit(account_type: str) -> float:
    """账户类型的单票仓位上限（占权益 %），供合规检查与仓位计算共用"""
    for key, limit in POSITION_LIMITS.items():
        if key in account_type or account_type in key:
            return limit
    return 8.0


def check_single_trade(
    trade: Trade,
    drawdown_state: str = DRAWDOWN_STATE,
    account_equity: float = ACCOUNT_EQUITY,
) -> list[Violation]:
    violations = []

    if trade.止损价 <= 0:
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="缺少止损",
            严重程度="高",
            描述="未设置止损价",
            建议="所有交易必须在入场前定义止损价",
        ))

    if BANNED_BOARD_PREFIXES and trade.股票代码:
        symbol = trade.股票代码.zfill(6)[-6:]
        if any(symbol.startswith(p) for p in BANNED_BOARD_PREFIXES):
            violations.append(Violation(
                交易编号=trade.交易编号,
                股票代码=trade.股票代码,
                违规类型="禁买板块",
                严重程度="高",
                描述=f"代码 {symbol} 命中禁买板块前缀 {'/'.join(BANNED_BOARD_PREFIXES)}（BANNED_BOARD_PREFIXES 配置）",
                建议="放弃该标的；确有把握需 --force 强制并在备注留痕",
            ))

    # 六条硬规则①：永久拉黑 N/C 字头新股次新（按名称前缀判定，名称为空无法判定时跳过）
    if BANNED_NEW_STOCK_ENABLED and trade.股票名称:
        if trade.股票名称.strip().upper()[:1] in ("N", "C"):
            violations.append(Violation(
                交易编号=trade.交易编号,
                股票代码=trade.股票代码,
                违规类型="禁买新股",
                严重程度="高",
                描述=f"「{trade.股票名称}」为 N/C 字头新股次新（六条硬规则：永久拉黑）",
                建议="放弃该标的；新股次新波动无规律，不纳入任何系统",
            ))

    risk_limit = get_risk_limit(trade.账户类型, drawdown_state)
    if trade.风险率 > MAX_SINGLE_RISK_PCT:
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="单笔风险超限",
            严重程度="高",
            描述=f"风险率 {trade.风险率}% 超过绝对上限 {MAX_SINGLE_RISK_PCT}%",
            建议="降低仓位或收紧止损",
        ))
    elif trade.风险率 > risk_limit:
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="单笔风险超限",
            严重程度="中",
            描述=f"风险率 {trade.风险率}% 超过 {drawdown_state} 状态下 {trade.账户类型} 账户上限 {risk_limit}%",
            建议="按回撤状态机降低风险",
        ))

    if account_equity > 0 and trade.仓位金额 > 0:
        position_pct = trade.仓位金额 / account_equity * 100
        pos_limit = get_position_limit(trade.账户类型)
        if position_pct > MAX_SINGLE_POSITION_PCT:
            # 六条硬规则②a：单票仓位绝对上限（独立于账户类型上限，高级违规）
            violations.append(Violation(
                交易编号=trade.交易编号,
                股票代码=trade.股票代码,
                违规类型="仓位超限",
                严重程度="高",
                描述=f"仓位 {position_pct:.1f}% 超过单票绝对上限 {MAX_SINGLE_POSITION_PCT}%（六条硬规则：仓位砍半）",
                建议="减仓至绝对上限以内；全仓模式任何一次判断失误都是满伤害",
            ))
        elif position_pct > pos_limit:
            violations.append(Violation(
                交易编号=trade.交易编号,
                股票代码=trade.股票代码,
                违规类型="仓位超限",
                严重程度="中",
                描述=f"仓位 {position_pct:.1f}% 超过 {trade.账户类型} 单票上限 {pos_limit}%",
                建议="减仓至上限以内",
            ))

    forbidden = FORBIDDEN_IN_DRAWDOWN.get(drawdown_state, [])
    for ftype in forbidden:
        if ftype in trade.账户类型 or ftype in trade.入场系统:
            violations.append(Violation(
                交易编号=trade.交易编号,
                股票代码=trade.股票代码,
                违规类型="回撤期禁止交易",
                严重程度="高",
                描述=f"当前回撤状态 {drawdown_state} 下禁止 {ftype} 类型交易",
                建议="等待回撤修复后再恢复该类型交易",
            ))
            break

    # 六条硬规则③：不追 09:30–10:00 开盘冲高（中级警告，事后审计同口径）
    entry_time = (trade.入场时间 or "").strip()
    if entry_time and entry_time[:5] < OPEN_CHASE_CUTOFF:
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="开盘追高",
            严重程度="中",
            描述=f"入场时间 {entry_time} 早于 {OPEN_CHASE_CUTOFF}（六条硬规则：放弃追开盘冲高）",
            建议="改为尾盘 14:30 后或盘中回调时买入",
        ))

    # 六条硬规则④：止损宽度建议 -3%~-4%（趋势系统 S1/S2 的 2N 止损为系统定义，豁免）
    _sys = (trade.入场系统 or "").upper()
    if (
        "S1" not in _sys and "S2" not in _sys
        and trade.入场价 > 0 and trade.止损价 > 0
    ):
        stop_width_pct = (trade.入场价 - trade.止损价) / trade.入场价 * 100
        if stop_width_pct > STOP_SUGGEST_MAX_PCT:
            violations.append(Violation(
                交易编号=trade.交易编号,
                股票代码=trade.股票代码,
                违规类型="止损过宽",
                严重程度="中",
                描述=f"止损宽度 {stop_width_pct:.1f}% 超过建议上限 {STOP_SUGGEST_MAX_PCT}%（六条硬规则：止损建议 -3%~-4%）",
                建议="收紧止损至 -3%~-4% 区间，或降低仓位保持单笔风险不变",
            ))

    # 六条硬规则⑥：三行记账——买入理由 / 止损位 / 目标位（止损位缺失已由「缺少止损」高级违规覆盖）
    if not (trade.核心逻辑 or "").strip():
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="缺少买入理由",
            严重程度="中",
            描述="未记录买入理由（六条硬规则：没有三行字不下单）",
            建议="建仓前写清买入理由（--logic / 核心逻辑）",
        ))
    if trade.目标价 is None or trade.目标价 <= 0:
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="缺少目标位",
            严重程度="中",
            描述="未记录目标价（六条硬规则：没有三行字不下单）",
            建议="建仓前写清目标位（--target / 目标价）",
        ))

    if not trade.是否系统内交易:
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="非系统内交易",
            严重程度="低",
            描述="该交易标记为非系统内交易",
            建议="复盘原因，避免重复",
        ))
    elif trade.入场系统 and trade.入场系统 not in STRATEGY_CODES:
        # 入场系统不在策略清单内（STRATEGY_CODES），同样视为非系统内交易
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="非系统内交易",
            严重程度="低",
            描述=f"入场系统「{trade.入场系统}」不在策略清单 {'/'.join(STRATEGY_CODES)} 内",
            建议="按策略评估筛选框架核对该交易的系统归属",
        ))

    return violations


def check_risk_cluster(
    trades: list[Trade],
    account_equity: float = ACCOUNT_EQUITY,
) -> list[Violation]:
    violations = []
    cluster_exposure: dict[str, float] = {}
    cluster_risk: dict[str, float] = {}

    for t in trades:
        if not t.风险簇:
            continue
        cluster = t.风险簇
        cluster_exposure[cluster] = cluster_exposure.get(cluster, 0) + (
            t.仓位金额 / account_equity * 100 if account_equity > 0 else 0
        )
        cluster_risk[cluster] = cluster_risk.get(cluster, 0) + t.风险率

    for cluster, exposure in cluster_exposure.items():
        limits = RISK_CLUSTER_LIMITS.get(cluster)
        if not limits:
            continue
        if exposure > limits["exposure"]:
            violations.append(Violation(
                交易编号="(组合)",
                股票代码="",
                违规类型="风险簇暴露超限",
                严重程度="高",
                描述=f"{cluster} 簇市值暴露 {exposure:.1f}% 超过上限 {limits['exposure']}%",
                建议="降低该风险簇总仓位",
            ))
        risk = cluster_risk.get(cluster, 0)
        if risk > limits["stop_risk"]:
            violations.append(Violation(
                交易编号="(组合)",
                股票代码="",
                违规类型="风险簇止损风险超限",
                严重程度="高",
                描述=f"{cluster} 簇止损风险合计 {risk:.2f}% 超过上限 {limits['stop_risk']}%",
                建议="减少该簇持仓或收紧止损",
            ))

    return violations


def _is_loss(t: Trade) -> bool:
    """已平仓笔是否亏损（优先取存储的 R倍数，否则按退出价与入场价比较）"""
    if t.R倍数 is not None:
        return t.R倍数 < 0
    return t.实际退出价 is not None and t.实际退出价 < t.入场价


def check_behavior_guards(
    trade: Trade,
    open_trades: list[Trade],
    closed_trades: list[Trade],
    today: date | None = None,
) -> list[Violation]:
    """
    六条硬规则的组合级行为门禁（建仓闸门专用；纯函数）。

    - 规则②b：同时持仓只数（按代码去重，加仓子单不重复计）+ 本笔 > MAX_OPEN_POSITIONS → 高级违规
    - 规则⑤：最近 N 笔已平仓全亏且最近退出日期距今 ≤ CONSECUTIVE_LOSS_HALT_DAYS → 高级违规
    - 规则⑥：本周（周一至当日）新开仓（排除加仓/拆单子单）+ 本笔 > MAX_WEEKLY_ENTRIES → 高级违规
    """
    today = today or date.today()
    violations = []

    # 规则②b：同时持仓只数上限
    sym = trade.股票代码.zfill(6)[-6:] if trade.股票代码 else ""
    open_symbols = {t.股票代码.zfill(6)[-6:] for t in open_trades if t.股票代码}
    position_count = len(open_symbols) + (0 if sym in open_symbols else 1)
    if position_count > MAX_OPEN_POSITIONS:
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="持仓数量超限",
            严重程度="高",
            描述=f"含本笔持仓 {position_count} 只，超过上限 {MAX_OPEN_POSITIONS} 只（六条硬规则：同时持有不超过 2 只）",
            建议="先平掉一只再开新仓",
        ))

    # 规则⑤：连亏停手
    recent_closed = sorted(
        [t for t in closed_trades if t.退出日期],
        key=lambda t: t.退出日期,
        reverse=True,
    )[:CONSECUTIVE_LOSS_HALT_COUNT]
    if len(recent_closed) == CONSECUTIVE_LOSS_HALT_COUNT and all(_is_loss(t) for t in recent_closed):
        last_exit = date.fromisoformat(recent_closed[0].退出日期)
        if 0 <= (today - last_exit).days <= CONSECUTIVE_LOSS_HALT_DAYS:
            violations.append(Violation(
                交易编号=trade.交易编号,
                股票代码=trade.股票代码,
                违规类型="连亏停手",
                严重程度="高",
                描述=(f"最近 {CONSECUTIVE_LOSS_HALT_COUNT} 笔已平仓全部亏损"
                      f"（最近退出 {recent_closed[0].退出日期}），停手 {CONSECUTIVE_LOSS_HALT_DAYS} 天内禁止新开仓"),
                建议="先复盘再找下一笔；亏损成簇出现时停手机制能直接砍掉一半连亏",
            ))

    # 规则⑥：每周新开仓频率上限
    monday = today - timedelta(days=today.weekday())
    week_entries = sum(
        1 for t in open_trades + closed_trades
        if not t.关联单号 and t.日期 >= monday.isoformat()
    )
    if week_entries + 1 > MAX_WEEKLY_ENTRIES:
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="交易频率超限",
            严重程度="高",
            描述=f"本周已新开仓 {week_entries} 笔，含本笔超过上限 {MAX_WEEKLY_ENTRIES} 笔/周（六条硬规则：降频）",
            建议="本周不再开新仓；降频直接降低费用与冲动交易",
        ))

    return violations


def check_market_conditions(
    trade: Trade,
    open_trades: list[Trade],
    market_state: str | None,
    account_equity: float = ACCOUNT_EQUITY,
    sentiment_phase: str | None = None,
) -> list[Violation]:
    """
    组合总热度 + 市场状态门禁 + 情绪周期门禁（V5.0 §5.6/§1.2，建仓闸门专用；纯函数）。

    - 账户热度：未平仓风险率合计 + 本笔风险率 > PORTFOLIO_HEAT_LIMITS[市场状态] → 高级违规
    - 市场状态 D：禁止新开趋势仓（入场系统含 S1/S2）→ 高级违规
    - 市场状态 C：趋势仓中级警告（震荡市建议风险减半）
    - 情绪相位 冰点/退潮：禁止新开超短仓（入场系统含 HOT/EVT）→ 高级违规（2026-09 情绪闸门）
    - market_state/sentiment_phase 为 None/未知：跳过对应检查（降级，不阻塞建仓）
    """
    violations = []
    if not market_state:
        return _check_sentiment_gate(trade, sentiment_phase)
    limit = PORTFOLIO_HEAT_LIMITS.get(str(market_state).upper())
    if limit is None:
        return _check_sentiment_gate(trade, sentiment_phase)

    total_heat = sum(t.风险率 for t in open_trades if t.风险率 > 0) + max(trade.风险率, 0)
    if total_heat > limit:
        violations.append(Violation(
            交易编号="(组合)",
            股票代码=trade.股票代码,
            违规类型="账户热度超限",
            严重程度="高",
            描述=f"总热度 {total_heat:.2f}%（未平仓风险合计 + 本笔）超过 {market_state} 状态上限 {limit}%",
            建议="降低本笔风险率，或先减仓释放热度",
        ))

    s = (trade.入场系统 or "").upper()
    is_trend = "S1" in s or "S2" in s
    state = str(market_state).upper()
    if state == "D" and is_trend:
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="市场状态禁止建仓",
            严重程度="高",
            描述="市场状态 D（系统性下跌）禁止新开趋势仓（V5.0 §1.2：D 状态总仓位 0%—30%）",
            建议="等待市场状态修复；D 状态只保留核心仓和极小验证仓",
        ))
    elif state == "C" and is_trend:
        violations.append(Violation(
            交易编号=trade.交易编号,
            股票代码=trade.股票代码,
            违规类型="市场状态提示",
            严重程度="中",
            描述="市场状态 C（震荡轮动），趋势信号胜率偏低，建议风险减半",
            建议="降低单笔风险率或等更好的市场状态",
        ))
    violations.extend(_check_sentiment_gate(trade, sentiment_phase))
    return violations


def _check_sentiment_gate(trade: Trade, sentiment_phase: str | None) -> list[Violation]:
    """情绪周期门禁：冰点/退潮 禁止新开超短仓（HOT-S/二板/EVT-S 等含 HOT/EVT 系统）"""
    if not sentiment_phase or sentiment_phase not in SENTIMENT_BAN_PHASES:
        return []
    s = (trade.入场系统 or "").upper()
    if not ("HOT" in s or "EVT" in s):
        return []
    return [Violation(
        交易编号=trade.交易编号,
        股票代码=trade.股票代码,
        违规类型="情绪相位禁止建仓",
        严重程度="高",
        描述=f"情绪周期「{sentiment_phase}」（超短生态：涨停/连板/炸板/晋级恶化），"
             f"禁止新开超短仓（{trade.入场系统}）",
        建议="冰点/退潮只管理已有持仓；等修复/发酵相位再开新仓（情绪闸门，2026-09）",
    )]


def run_compliance_check(
    trades: list[Trade] | None = None,
    drawdown_state: str = DRAWDOWN_STATE,
    account_equity: float = ACCOUNT_EQUITY,
    trade_log: TradeLog | None = None,
) -> ComplianceReport:
    if trades is None:
        trade_log = trade_log or TradeLog()
        trades = trade_log.list_all()

    report = ComplianceReport(
        检查交易数=len(trades),
        回撤状态=drawdown_state,
    )

    for trade in trades:
        for v in check_single_trade(trade, drawdown_state, account_equity):
            report.add(v)

    for v in check_risk_cluster(trades, account_equity):
        report.add(v)

    report.违规笔数 = len({v.交易编号 for v in report.违规列表 if v.交易编号 != "(组合)"})
    return report


def report_to_markdown(report: ComplianceReport) -> str:
    lines = [
        "## 合规检查报告",
        "",
        "| 项目 | 数值 |",
        "| --- | --- |",
        f"| 检查交易数 | {report.检查交易数} |",
        f"| 违规笔数 | {report.违规笔数} |",
        f"| 合规率 | {report.合规率}% |",
        f"| 回撤状态 | {report.回撤状态} |",
        "",
    ]

    if not report.违规列表:
        lines.append("[PASS] 未发现违规项")
    else:
        lines.append("### 违规明细")
        lines.append("")
        lines.append("| 交易编号 | 代码 | 类型 | 严重程度 | 描述 | 建议 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for v in report.违规列表:
            lines.append(
                f"| {v.交易编号} | {v.股票代码} | {v.违规类型} | {v.严重程度} | {v.描述} | {v.建议} |"
            )

    return "\n".join(lines)
