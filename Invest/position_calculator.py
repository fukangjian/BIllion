"""
仓位计算器 — 风险预算法 + 风险簇检查 + 跳空折扣

核心公式:
    风险预算R = 账户权益 × 风险比例
    每股风险 = (入场价 - 止损价) × 跳空折扣 + 跳空缓冲
    股数 = floor(R / 每股风险 / 100) × 100

用法:
    python position_calculator.py --symbol 600519 --entry 1800 --stop 1700 \\
        --account-type core --account-state normal --equity 1000000
"""
import argparse
import json
import math
import warnings

warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")
import sys
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class AccountType(str, Enum):
    """账户类型"""
    CORE = "core"           # 核心趋势
    INDUSTRY = "industry"   # 产业趋势
    EVENT = "event"         # 事件交易
    SEED = "seed"           # 预埋验证


class AccountState(str, Enum):
    """账户状态"""
    NORMAL = "normal"       # 正常
    ALERT = "alert"         # 警戒（回撤期）
    DEFENSE = "defense"     # 防御


class GapRisk(str, Enum):
    """跳空风险类型"""
    NONE = "none"                   # 无特殊跳空风险
    ANNOUNCEMENT = "announcement"   # 一般公告窗口
    CLINICAL = "clinical"           # 临床关键数据前
    GEOPOLITICAL = "geopolitical"   # 极端地缘事件股


# 风险率表 (百分比)
RISK_RATES = {
    AccountType.CORE: {
        AccountState.NORMAL: (0.004, 0.006),    # 0.4%-0.6%
        AccountState.ALERT: (0.002, 0.003),     # 0.2%-0.3%
        AccountState.DEFENSE: (0.001, 0.002),
    },
    AccountType.INDUSTRY: {
        AccountState.NORMAL: (0.003, 0.005),
        AccountState.ALERT: (0.0015, 0.0025),
        AccountState.DEFENSE: (0.001, 0.0015),
    },
    AccountType.EVENT: {
        AccountState.NORMAL: (0.002, 0.0035),
        AccountState.ALERT: (0.001, 0.0018),
        AccountState.DEFENSE: (0.0005, 0.001),
    },
    AccountType.SEED: {
        AccountState.NORMAL: (0.001, 0.0015),
        AccountState.ALERT: (0.0, 0.0),         # 回撤期暂停
        AccountState.DEFENSE: (0.0, 0.0),
    },
}

# 跳空折扣
GAP_DISCOUNT = {
    GapRisk.NONE: 1.0,
    GapRisk.ANNOUNCEMENT: 0.7,
    GapRisk.CLINICAL: 0.5,
    GapRisk.GEOPOLITICAL: 0.6,
}

# 单票仓位上限 (占总权益比例)
SINGLE_STOCK_LIMIT = {
    AccountType.CORE: 0.10,
    AccountType.INDUSTRY: 0.08,
    AccountType.EVENT: 0.05,
    AccountType.SEED: 0.03,
}

# 风险簇上限
CLUSTER_LIMITS = {
    "same_industry": 0.25,      # 同行业总仓位
    "innovative_pharma": 0.25,  # 创新药总仓位
    "total_heat": 0.03,         # 全部持仓同时止损的理论损失
}

# 行业映射（示例，可扩展）
INDUSTRY_MAP = {
    "600519": "消费",
    "000858": "消费",
    "300760": "医药",
    "688235": "创新药",
    "688331": "创新药",
    "300750": "新能源",
    "002594": "新能源",
}


@dataclass
class Position:
    """持仓信息"""
    symbol: str
    shares: int
    entry_price: float
    stop_price: float
    industry: str = ""
    is_innovative_pharma: bool = False


@dataclass
class PositionResult:
    """仓位计算结果"""
    symbol: str
    entry_price: float
    stop_price: float
    shares: int
    position_value: float
    position_pct: float
    risk_budget: float
    risk_per_share: float
    risk_rate_used: float
    gap_discount: float
    exceeds_single_limit: bool
    single_limit_pct: float
    cluster_warnings: list = field(default_factory=list)
    heat_check_pass: bool = True
    heat_loss_pct: float = 0.0
    account_type: str = ""
    account_state: str = ""
    notes: list = field(default_factory=list)


def get_risk_rate(
    account_type: AccountType,
    account_state: AccountState,
    use_conservative: bool = True,
) -> float:
    """
    获取风险比例
    use_conservative: True 取区间下限，False 取中值
    """
    rates = RISK_RATES.get(account_type, {}).get(account_state, (0.002, 0.003))
    if account_state in (AccountState.ALERT, AccountState.DEFENSE) or use_conservative:
        return rates[0]
    return (rates[0] + rates[1]) / 2


def calc_shares(
    equity: float,
    entry_price: float,
    stop_price: float,
    account_type: AccountType,
    account_state: AccountState,
    gap_risk: GapRisk = GapRisk.NONE,
    gap_buffer: float = 0.0,
    use_conservative: bool = True,
) -> PositionResult:
    """
    计算建议股数

    参数:
        equity: 账户权益
        entry_price: 入场价
        stop_price: 止损价
        account_type: 账户类型
        account_state: 账户状态
        gap_risk: 跳空风险类型
        gap_buffer: 额外跳空缓冲（绝对价格）
        use_conservative: 是否使用保守风险率
    """
    notes = []
    risk_rate = get_risk_rate(account_type, account_state, use_conservative)

    if risk_rate <= 0:
        return PositionResult(
            symbol="",
            entry_price=entry_price,
            stop_price=stop_price,
            shares=0,
            position_value=0,
            position_pct=0,
            risk_budget=0,
            risk_per_share=0,
            risk_rate_used=0,
            gap_discount=0,
            exceeds_single_limit=False,
            single_limit_pct=SINGLE_STOCK_LIMIT.get(account_type, 0.1),
            account_type=account_type.value,
            account_state=account_state.value,
            notes=["回撤期暂停交易（预埋账户）"],
        )

    gap_discount = GAP_DISCOUNT.get(gap_risk, 1.0)
    risk_budget = equity * risk_rate

    # 每股风险
    price_risk = max(entry_price - stop_price, 0)
    if price_risk <= 0:
        notes.append("警告: 止损价 >= 入场价，无法计算")
        price_risk = entry_price * 0.02  # 默认 2% 止损距离

    risk_per_share = price_risk * gap_discount + gap_buffer

    # 股数（A股100股一手）
    raw_shares = risk_budget / risk_per_share if risk_per_share > 0 else 0
    shares = math.floor(raw_shares / 100) * 100

    position_value = shares * entry_price
    position_pct = position_value / equity if equity > 0 else 0
    single_limit = SINGLE_STOCK_LIMIT.get(account_type, 0.10)
    exceeds = position_pct > single_limit

    if exceeds and shares > 0:
        # 按上限反算
        max_value = equity * single_limit
        shares = math.floor(max_value / entry_price / 100) * 100
        position_value = shares * entry_price
        position_pct = position_value / equity
        notes.append(f"已按单票上限 {single_limit*100:.0f}% 缩减股数")

    if shares <= 0 and raw_shares > 0:
        notes.append(
            f"风险预算仅够 {int(raw_shares)} 股，不足 A 股最小交易单位(100股)，建议放宽止损或提高风险比例"
        )

    if gap_risk != GapRisk.NONE:
        notes.append(f"跳空折扣 {gap_discount} 已应用 ({gap_risk.value})")

    return PositionResult(
        symbol="",
        entry_price=entry_price,
        stop_price=stop_price,
        shares=shares,
        position_value=position_value,
        position_pct=position_pct,
        risk_budget=risk_budget,
        risk_per_share=risk_per_share,
        risk_rate_used=risk_rate,
        gap_discount=gap_discount,
        exceeds_single_limit=exceeds,
        single_limit_pct=single_limit,
        account_type=account_type.value,
        account_state=account_state.value,
        notes=notes,
    )


def check_cluster_risk(
    result: PositionResult,
    existing_positions: list[Position],
    new_symbol: str,
    new_industry: str = "",
    is_innovative_pharma: bool = False,
    equity: float = 1_000_000,
) -> PositionResult:
    """
    风险簇检查 — 同行业、创新药、账户热度
    """
    result.symbol = new_symbol
    warnings = []

    # 同行业检查
    industry_exposure = result.position_value
    for pos in existing_positions:
        if pos.industry == new_industry and new_industry:
            industry_exposure += pos.shares * pos.entry_price

    if new_industry:
        industry_pct = industry_exposure / equity
        if industry_pct > CLUSTER_LIMITS["same_industry"]:
            warnings.append(
                f"同行业({new_industry})总仓位 {industry_pct*100:.1f}% "
                f"超过上限 {CLUSTER_LIMITS['same_industry']*100:.0f}%"
            )

    # 创新药检查
    if is_innovative_pharma:
        pharma_exposure = result.position_value if is_innovative_pharma else 0
        for pos in existing_positions:
            if pos.is_innovative_pharma:
                pharma_exposure += pos.shares * pos.entry_price
        pharma_pct = pharma_exposure / equity
        if pharma_pct > CLUSTER_LIMITS["innovative_pharma"]:
            warnings.append(
                f"创新药总仓位 {pharma_pct*100:.1f}% "
                f"超过上限 {CLUSTER_LIMITS['innovative_pharma']*100:.0f}%"
            )

    # 账户热度检查 — 所有持仓同时触发止损的理论损失
    total_heat_loss = result.shares * result.risk_per_share
    for pos in existing_positions:
        pos_risk = (pos.entry_price - pos.stop_price) * pos.shares
        total_heat_loss += max(pos_risk, 0)

    heat_pct = total_heat_loss / equity if equity > 0 else 0
    heat_pass = heat_pct <= CLUSTER_LIMITS["total_heat"]

    if not heat_pass:
        warnings.append(
            f"账户热度 {heat_pct*100:.2f}% 超过上限 {CLUSTER_LIMITS['total_heat']*100:.0f}%"
        )

    result.cluster_warnings = warnings
    result.heat_check_pass = heat_pass
    result.heat_loss_pct = heat_pct
    return result


def format_result(result: PositionResult) -> str:
    """格式化输出结果"""
    lines = [
        "=" * 50,
        "仓位计算结果",
        "=" * 50,
        f"  股票代码:     {result.symbol}",
        f"  账户类型:     {result.account_type}",
        f"  账户状态:     {result.account_state}",
        f"  入场价:       {result.entry_price:.2f}",
        f"  止损价:       {result.stop_price:.2f}",
        "-" * 50,
        f"  风险比例:     {result.risk_rate_used*100:.2f}%",
        f"  风险预算:     {result.risk_budget:,.0f} 元",
        f"  每股风险:     {result.risk_per_share:.2f} 元",
        f"  跳空折扣:     {result.gap_discount}",
        "-" * 50,
        f"  建议股数:     {result.shares}",
        f"  仓位金额:     {result.position_value:,.0f} 元",
        f"  仓位比例:     {result.position_pct*100:.2f}%",
        f"  单票上限:     {result.single_limit_pct*100:.0f}%",
        f"  超限:         {'是' if result.exceeds_single_limit else '否'}",
        "-" * 50,
        f"  账户热度:     {result.heat_loss_pct*100:.2f}% (上限 {CLUSTER_LIMITS['total_heat']*100:.0f}%)",
        f"  热度检查:     {'通过' if result.heat_check_pass else '未通过'}",
    ]

    if result.cluster_warnings:
        lines.append("-" * 50)
        lines.append("  风险簇警告:")
        for w in result.cluster_warnings:
            lines.append(f"    ⚠ {w}")

    if result.notes:
        lines.append("-" * 50)
        lines.append("  备注:")
        for n in result.notes:
            lines.append(f"    · {n}")

    lines.append("=" * 50)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="仓位计算器 — 风险预算法")
    parser.add_argument("--symbol", "-s", required=True, help="股票代码")
    parser.add_argument("--entry", "-e", type=float, required=True, help="入场价")
    parser.add_argument("--stop", type=float, required=True, help="止损价")
    parser.add_argument("--equity", type=float, default=1_000_000, help="账户权益（默认100万）")
    parser.add_argument(
        "--account-type", "-t",
        choices=[t.value for t in AccountType],
        default="core",
        help="账户类型: core/industry/event/seed",
    )
    parser.add_argument(
        "--account-state",
        choices=[s.value for s in AccountState],
        default="normal",
        help="账户状态: normal/alert/defense",
    )
    parser.add_argument(
        "--gap-risk",
        choices=[g.value for g in GapRisk],
        default="none",
        help="跳空风险: none/announcement/clinical/geopolitical",
    )
    parser.add_argument("--gap-buffer", type=float, default=0.0, help="额外跳空缓冲（价格）")
    parser.add_argument("--industry", default="", help="所属行业")
    parser.add_argument("--innovative-pharma", action="store_true", help="是否创新药")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument(
        "--positions-file",
        type=str,
        default="",
        help="现有持仓 JSON 文件路径",
    )
    args = parser.parse_args()

    account_type = AccountType(args.account_type)
    account_state = AccountState(args.account_state)
    gap_risk = GapRisk(args.gap_risk)
    industry = args.industry or INDUSTRY_MAP.get(args.symbol, "")

    result = calc_shares(
        equity=args.equity,
        entry_price=args.entry,
        stop_price=args.stop,
        account_type=account_type,
        account_state=account_state,
        gap_risk=gap_risk,
        gap_buffer=args.gap_buffer,
    )

    # 加载现有持仓
    existing = []
    if args.positions_file:
        try:
            with open(args.positions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for p in data:
                    existing.append(Position(**p))
        except Exception as e:
            print(f"警告: 无法加载持仓文件: {e}", file=sys.stderr)

    result = check_cluster_risk(
        result,
        existing,
        new_symbol=args.symbol,
        new_industry=industry,
        is_innovative_pharma=args.innovative_pharma,
        equity=args.equity,
    )

    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    else:
        print(format_result(result))


if __name__ == "__main__":
    main()
