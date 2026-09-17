"""
入场合规闸门 — 建仓前的单笔 + 组合级合规检查

回撤状态优先取 monitor.derive_drawdown_state 推导值，推导失败降级 config.DRAWDOWN_STATE。
市场状态取 market_state 表最新值，读取失败/无数据降级跳过市场状态检查（不阻塞建仓）。
高级违规应拒绝写入 trades.json（调用方负责），--force 强制写入须在备注留痕。
"""
import logging
from pathlib import Path

from config import ACCOUNT_EQUITY, DRAWDOWN_STATE
from review.compliance_check import (
    Violation,
    check_behavior_guards,
    check_market_conditions,
    check_risk_cluster,
    check_single_trade,
)
from review.trade_log import Trade, TradeLog

logger = logging.getLogger(__name__)


def derive_state_safe(trade_log: TradeLog | None = None) -> str:
    """回撤状态：优先 monitor 推导值，推导失败降级 config.DRAWDOWN_STATE"""
    log = trade_log or TradeLog()
    try:
        from review.monitor import derive_drawdown_state

        return derive_drawdown_state(log)["state"]
    except Exception as e:
        logger.warning("回撤状态推导失败，降级使用 config.DRAWDOWN_STATE=%s: %s", DRAWDOWN_STATE, e)
        return DRAWDOWN_STATE


def get_latest_market_state(db_path: Path | None = None) -> str | None:
    """最新市场状态（market_state 表，离线）；无数据/读取失败返回 None（降级跳过状态检查）"""
    try:
        from pipeline.database import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT state FROM market_state ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        logger.warning("市场状态读取失败（跳过市场状态检查）: %s", e)
        return None


def get_latest_sentiment_phase(db_path: Path | None = None) -> str | None:
    """最新情绪周期相位（emotion_state 表，离线）；无数据/未知/读取失败返回 None（降级跳过情绪闸门）"""
    try:
        from pipeline.sentiment_regime import get_latest_phase

        row = get_latest_phase(db_path=db_path)
        if not row or not row.get("phase") or row.get("phase") == "未知":
            return None
        return row["phase"]
    except Exception as e:
        logger.warning("情绪相位读取失败（跳过情绪闸门）: %s", e)
        return None


def check_entry(
    trade: Trade,
    trade_log: TradeLog | None = None,
    account_equity: float = ACCOUNT_EQUITY,
    drawdown_state: str | None = None,
    market_state: str | None = None,
    db_path: Path | None = None,
    sentiment_phase: str | None = None,
) -> tuple[list[Violation], str]:
    """
    建仓合规闸门：单笔检查 + 含本笔假设建仓的组合级风险簇检查
    + 组合总热度/市场状态门禁（V5.0 §5.6，market_state 显式传入可跳过 DB 读取）
    + 情绪周期门禁（冰点/退潮禁开超短仓，sentiment_phase 显式传入可跳过 DB 读取）
    + 六条硬规则行为门禁（持仓只数/连亏停手/周频率）。

    返回 (违规列表, 实际使用的回撤状态)。
    """
    log = trade_log or TradeLog()
    state = drawdown_state or derive_state_safe(log)

    violations = check_single_trade(trade, drawdown_state=state, account_equity=account_equity)
    all_trades = log.list_all()
    open_trades = [t for t in all_trades if not t.is_closed]
    closed_trades = [t for t in all_trades if t.is_closed]
    violations.extend(check_risk_cluster(open_trades + [trade], account_equity=account_equity))
    # 六条硬规则组合级门禁：持仓只数 / 连亏停手 / 周频率
    violations.extend(check_behavior_guards(trade, open_trades, closed_trades))

    if market_state is None:
        market_state = get_latest_market_state(db_path)
    if sentiment_phase is None:
        sentiment_phase = get_latest_sentiment_phase(db_path)
    violations.extend(check_market_conditions(
        trade, open_trades, market_state, account_equity, sentiment_phase=sentiment_phase))
    return violations, state


def split_by_severity(violations: list[Violation]) -> tuple[list[Violation], list[Violation]]:
    """按严重程度拆分，返回 (高级违规, 中/低级违规)"""
    high = [v for v in violations if v.严重程度 == "高"]
    others = [v for v in violations if v.严重程度 != "高"]
    return high, others


def format_violations(violations: list[Violation]) -> str:
    """违规列表转打印文本"""
    return "\n".join(
        f"  [{v.严重程度}] {v.违规类型}: {v.描述} → {v.建议}" for v in violations
    )


def force_note(violations: list[Violation]) -> str:
    """--force 强制建仓的备注留痕文本"""
    kinds = "；".join(v.违规类型 for v in violations)
    return f"⚠️ 强制建仓，违规：{kinds}"
