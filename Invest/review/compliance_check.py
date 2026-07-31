"""
规则偏差检查 — 基于投资体系 V5.0 的合规审计
"""
import logging
from dataclasses import dataclass, field

from config import (
    ACCOUNT_EQUITY,
    DRAWDOWN_STATE,
    FORBIDDEN_IN_DRAWDOWN,
    MAX_SINGLE_RISK_PCT,
    POSITION_LIMITS,
    RISK_CLUSTER_LIMITS,
    RISK_LIMITS_DRAWDOWN,
    RISK_LIMITS_NORMAL,
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
        if position_pct > pos_limit:
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
        # 入场系统不在五策略清单内（STRATEGY_CODES），同样视为非系统内交易
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
