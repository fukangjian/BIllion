"""
仓位计算器 — 风险预算法 + 风险簇检查 + 跳空折扣

风险率 / 单票仓位上限 / 风险簇上限统一读取 config（投资体系 V5.0 口径），
账户类型使用中文枚举（核心/产业/事件/实验），与合规检查共用同一套限额函数。

核心公式:
    风险预算R = 账户权益 × 风险比例
    每股风险 = (入场价 - 止损价) × 跳空折扣 + 跳空缓冲
    股数 = floor(R / 每股风险 / 100) × 100

用法:
    python position_calculator.py --symbol 600519 --entry 1800 --stop 1700 \
        --account-type 核心 --drawdown-state Normal --equity 1000000
"""
import argparse
import json
import math
import sys
import warnings
from enum import Enum
from pathlib import Path

warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import (
    ACCOUNT_EQUITY,
    ACCOUNT_TYPES,
    ADD_SPACING_MAX,
    ADD_SPACING_MIN,
    DRAWDOWN_STATE,
    INDUSTRY_MAP,
)
from review.compliance_check import check_risk_cluster, get_position_limit, get_risk_limit


class GapRisk(str, Enum):
    """跳空风险类型"""
    NONE = "none"                   # 无特殊跳空风险
    ANNOUNCEMENT = "announcement"   # 一般公告窗口
    CLINICAL = "clinical"           # 临床关键数据前
    GEOPOLITICAL = "geopolitical"   # 极端地缘事件股


# 跳空折扣
GAP_DISCOUNT = {
    GapRisk.NONE: 1.0,
    GapRisk.ANNOUNCEMENT: 0.7,
    GapRisk.CLINICAL: 0.5,
    GapRisk.GEOPOLITICAL: 0.6,
}

DRAWDOWN_STATES = ["Normal", "Caution", "Defensive", "Review"]


def lookup_cluster(symbol: str) -> str | None:
    """股票代码 → 风险簇（config.INDUSTRY_MAP），查不到返回 None（建仓时提示人工指定）"""
    return INDUSTRY_MAP.get(str(symbol).zfill(6)[-6:])


def calc_position(
    equity: float,
    entry: float,
    stop: float,
    account_type: str,
    drawdown_state: str,
    gap_risk: GapRisk = GapRisk.NONE,
    gap_buffer: float = 0.0,
    atr: float | None = None,
) -> dict:
    """
    仓位计算纯函数（可 import），风险率与单票上限取 config 口径。

    参数:
        equity: 账户权益
        entry: 入场价
        stop: 止损价
        account_type: 账户类型（核心/产业/事件/实验）
        drawdown_state: 回撤状态（Normal/Caution/Defensive/Review）
        gap_risk: 跳空风险类型
        gap_buffer: 额外跳空缓冲（绝对价格）
        atr: ATR(20)，用于加仓价建议（加仓价 = 入场 + ADD_SPACING_MIN/ADD_SPACING_MAX × N）；
             未提供时以每股风险为 N 估算

    返回:
        dict：账户类型/回撤状态/账户权益/入场价/止损价/风险率/风险预算/每股风险/
              跳空折扣/股数/仓位金额/仓位比例/单票上限/是否超限/加仓价1/加仓价2/备注
    """
    notes: list[str] = []
    risk_rate = get_risk_limit(account_type, drawdown_state)
    single_limit = get_position_limit(account_type)

    base = {
        "账户类型": account_type,
        "回撤状态": drawdown_state,
        "账户权益": equity,
        "入场价": entry,
        "止损价": stop,
        "单票上限": single_limit,
    }

    if risk_rate <= 0:
        return {
            **base,
            "风险率": 0.0,
            "风险预算": 0.0,
            "每股风险": 0.0,
            "跳空折扣": 0.0,
            "股数": 0,
            "仓位金额": 0.0,
            "仓位比例": 0.0,
            "是否超限": False,
            "加仓价1": None,
            "加仓价2": None,
            "备注": [f"{drawdown_state} 状态下 {account_type} 风险率上限为 0，暂停该类型交易"],
        }

    gap_discount = GAP_DISCOUNT.get(gap_risk, 1.0)
    risk_budget = equity * risk_rate / 100

    # 每股风险
    price_risk = max(entry - stop, 0)
    if price_risk <= 0:
        notes.append("警告: 止损价 >= 入场价，按默认 2% 止损距离估算")
        price_risk = entry * 0.02

    risk_per_share = price_risk * gap_discount + gap_buffer

    # 股数（A股100股一手）
    raw_shares = risk_budget / risk_per_share if risk_per_share > 0 else 0
    shares = math.floor(raw_shares / 100) * 100

    position_value = shares * entry
    position_pct = position_value / equity * 100 if equity > 0 else 0
    exceeds = position_pct > single_limit

    if exceeds and shares > 0:
        # 按上限反算
        max_value = equity * single_limit / 100
        shares = math.floor(max_value / entry / 100) * 100
        position_value = shares * entry
        position_pct = position_value / equity * 100 if equity > 0 else 0
        notes.append(f"已按单票上限 {single_limit:.0f}% 缩减股数")

    if shares <= 0 and raw_shares > 0:
        notes.append(
            f"风险预算仅够 {int(raw_shares)} 股，不足 A 股最小交易单位(100股)，建议放宽止损或提高风险比例"
        )

    if gap_risk != GapRisk.NONE:
        notes.append(f"跳空折扣 {gap_discount} 已应用 ({gap_risk.value})")

    # 加仓价建议：N 优先取 ATR，未提供时以每股风险估算
    n = atr if atr and atr > 0 else risk_per_share
    add1 = round(entry + ADD_SPACING_MIN * n, 2)
    add2 = round(entry + ADD_SPACING_MAX * n, 2)

    return {
        **base,
        "风险率": risk_rate,
        "风险预算": risk_budget,
        "每股风险": risk_per_share,
        "跳空折扣": gap_discount,
        "股数": shares,
        "仓位金额": position_value,
        "仓位比例": position_pct,
        "是否超限": exceeds,
        "加仓价1": add1,
        "加仓价2": add2,
        "备注": notes,
    }


def format_calc_text(calc: dict, symbol: str = "") -> str:
    """格式化仓位计算结果（CLI 与 from-scan 打印共用）"""
    lines = [
        "=" * 50,
        "仓位计算结果",
        "=" * 50,
    ]
    if symbol:
        lines.append(f"  股票代码:     {symbol}")
    lines += [
        f"  账户类型:     {calc['账户类型']}",
        f"  回撤状态:     {calc['回撤状态']}",
        f"  入场价:       {calc['入场价']:.2f}",
        f"  止损价:       {calc['止损价']:.2f}",
        "-" * 50,
        f"  风险比例:     {calc['风险率']:.2f}%",
        f"  风险预算:     {calc['风险预算']:,.0f} 元",
        f"  每股风险:     {calc['每股风险']:.2f} 元",
        f"  跳空折扣:     {calc['跳空折扣']}",
        "-" * 50,
        f"  建议股数:     {calc['股数']}",
        f"  仓位金额:     {calc['仓位金额']:,.0f} 元",
        f"  仓位比例:     {calc['仓位比例']:.2f}%",
        f"  单票上限:     {calc['单票上限']:.0f}%",
        f"  超限:         {'是（已缩减）' if calc['是否超限'] else '否'}",
    ]
    if calc.get("加仓价1") is not None:
        lines += [
            "-" * 50,
            f"  加仓价 1:     {calc['加仓价1']:.2f}（首仓 + {ADD_SPACING_MIN:g}N）",
            f"  加仓价 2:     {calc['加仓价2']:.2f}（首仓 + {ADD_SPACING_MAX:g}N）",
        ]

    if calc.get("备注"):
        lines.append("-" * 50)
        lines.append("  备注:")
        for n in calc["备注"]:
            lines.append(f"    · {n}")

    lines.append("=" * 50)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="仓位计算器 — 风险预算法（config 口径）")
    parser.add_argument("--symbol", "-s", required=True, help="股票代码")
    parser.add_argument("--entry", "-e", type=float, required=True, help="入场价")
    parser.add_argument("--stop", type=float, required=True, help="止损价")
    parser.add_argument("--equity", type=float, default=ACCOUNT_EQUITY, help="账户权益（默认 config.ACCOUNT_EQUITY）")
    parser.add_argument(
        "--account-type", "-t",
        choices=ACCOUNT_TYPES,
        default="核心",
        help="账户类型: 核心/产业/事件/实验",
    )
    parser.add_argument(
        "--drawdown-state",
        choices=DRAWDOWN_STATES,
        default=DRAWDOWN_STATE,
        help="回撤状态: Normal/Caution/Defensive/Review（默认 config.DRAWDOWN_STATE）",
    )
    parser.add_argument(
        "--gap-risk",
        choices=[g.value for g in GapRisk],
        default="none",
        help="跳空风险: none/announcement/clinical/geopolitical",
    )
    parser.add_argument("--gap-buffer", type=float, default=0.0, help="额外跳空缓冲（价格）")
    parser.add_argument("--atr", type=float, default=None, help="ATR(20)，用于加仓价建议（默认以每股风险为 N）")
    parser.add_argument("--cluster", default="", help="风险簇（默认查 config.INDUSTRY_MAP）")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument(
        "--check-existing",
        action="store_true",
        help="从 trades.json 加载已有持仓，做组合级风险簇检查（复用 compliance_check）",
    )
    args = parser.parse_args()

    calc = calc_position(
        equity=args.equity,
        entry=args.entry,
        stop=args.stop,
        account_type=args.account_type,
        drawdown_state=args.drawdown_state,
        gap_risk=GapRisk(args.gap_risk),
        gap_buffer=args.gap_buffer,
        atr=args.atr,
    )

    cluster_violations = []
    cluster = args.cluster or lookup_cluster(args.symbol)
    if args.check_existing:
        # 组合级风险簇检查：现有未平仓 + 本笔假设建仓，复用 compliance_check 逻辑
        from review.trade_log import Trade, TradeLog

        try:
            log = TradeLog()
            hypothetical = Trade(
                股票代码=args.symbol,
                账户类型=args.account_type,
                风险簇=cluster or "未指定",
                入场系统="",
                入场价=args.entry,
                止损价=args.stop,
                风险率=calc["风险率"],
                股数=calc["股数"],
                仓位金额=calc["仓位金额"],
            )
            cluster_violations = check_risk_cluster(
                log.list_all(open_only=True) + [hypothetical],
                account_equity=args.equity,
            )
        except Exception as e:
            print(f"警告: 无法从 trades.json 加载持仓做簇风险检查: {e}", file=sys.stderr)
    elif not cluster:
        print(f"提示: {args.symbol} 不在 config.INDUSTRY_MAP 中，风险簇需人工指定（--cluster）", file=sys.stderr)

    if args.json:
        out = dict(calc)
        out["股票代码"] = args.symbol
        out["风险簇"] = cluster or "未指定"
        out["簇风险违规"] = [v.描述 for v in cluster_violations]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(format_calc_text(calc, symbol=args.symbol))
        if cluster_violations:
            print("风险簇警告:")
            for v in cluster_violations:
                print(f"  ⚠ [{v.严重程度}] {v.描述} → {v.建议}")


if __name__ == "__main__":
    main()
