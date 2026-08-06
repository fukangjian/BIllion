"""
金字塔加仓与统一止损规则（V5.0 §5.5 海龟三档，实盘侧纯函数）

与回测同一口径（next_add_action 复用 backtest/strategies.py）：
每涨 0.5N 加一单位（单根跳空 >1N 不追，基准推进、跳过单位不补），
最多 MAX_UNITS 单位；加仓成交后全部未平仓单位止损统一上移至
最新单位入场价 − 2N（只上不下）。

行情与成交均为人工确认后的离线判定：工具只算触发价与建议股数，
下单与回填由人执行（与体系「告警而非自动重仓」定位一致）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import ADD_SPACING_MAX, ADD_SPACING_MIN, ATR_STOP_MULT, MAX_UNITS
from backtest.strategies import next_add_action
from review.trade_log import Trade

# V5.0 §5.5 三档风险占比：首仓 40% / 第2仓 30% / 第3仓 30%（计划总风险）
ADD_UNIT_RISK_RATIO = 30 / 40  # 加仓单位风险预算 = 首仓实际风险金额 × 30/40


def get_unit_chain(open_trades: list[Trade], symbol: str) -> list[Trade]:
    """
    该代码的未平仓单位链：最新首仓 + 其全部加仓单位，按单位序号升序。
    无持仓返回空列表。
    """
    sym = str(symbol).zfill(6)[-6:]
    opens = [t for t in open_trades if not t.is_closed
             and str(t.股票代码).zfill(6)[-6:] == sym]
    roots = [t for t in opens if not t.关联单号]
    if not roots:
        return []
    root = max(roots, key=lambda t: (t.日期 or "", t.创建时间 or ""))
    chain = [root] + [t for t in opens if t.关联单号 == root.交易编号]
    return sorted(chain, key=lambda t: (t.单位序号 or 1, t.创建时间 or ""))


def check_add_trigger(chain: list[Trade], latest_close: float, atr: float) -> dict:
    """
    加仓触发判定（纯函数）。

    返回:
        可加仓 / 原因 / 触发价（=建议加仓成交价基准）/ 下一单位序号 /
        新基准价 / 统一止损价（加仓成交后全链上移至 新入场价 − 2N）
    """
    result = {
        "可加仓": False, "原因": "", "触发价": None,
        "下一单位序号": None, "新基准价": None, "统一止损价": None,
    }
    if not chain:
        result["原因"] = "无未平仓单位链（需先有首仓）"
        return result
    if not atr or atr <= 0:
        result["原因"] = "ATR 不可用，无法判定加仓间距"
        return result

    units = len(chain)
    if units >= MAX_UNITS:
        result["原因"] = f"已达最大单位数 {MAX_UNITS}（V5.0 海龟三档），不再加仓"
        return result

    last_price = chain[-1].入场价
    should_add, new_baseline = next_add_action(
        latest_close, last_price, atr, ADD_SPACING_MIN, ADD_SPACING_MAX,
    )
    if should_add:
        result.update({
            "可加仓": True,
            "原因": f"现价较上次入场价 {last_price:.2f} 上涨 ≥ {ADD_SPACING_MIN:g}N",
            "触发价": round(latest_close, 2),
            "下一单位序号": units + 1,
            "新基准价": round(latest_close, 2),
            "统一止损价": unified_stop_after_add(latest_close, atr),
        })
    elif new_baseline != last_price:
        result.update({
            "原因": f"单根跳空超 {ADD_SPACING_MAX:g}N 不追，加仓基准推进至 {new_baseline:.2f}（跳过单位不补）",
            "新基准价": round(new_baseline, 2),
        })
    else:
        trigger = last_price + ADD_SPACING_MIN * atr
        result.update({
            "原因": f"未达加仓间距（触发价 {trigger:.2f}，还差 {trigger - latest_close:.2f}）",
            "新基准价": round(last_price, 2),
        })
    return result


def unified_stop_after_add(new_unit_entry: float, atr: float) -> float:
    """海龟统一止损：加仓后全部未平仓单位止损上移至 最新入场价 − 2N（调用方负责只上不下）"""
    return round(new_unit_entry - atr * ATR_STOP_MULT, 2)


def add_unit_shares(first_unit: Trade, per_share_risk: float) -> int:
    """
    加仓单位建议股数 = 首仓实际风险金额 × (30/40) ÷ 加仓单位每股风险，整手向下取整
    （V5.0 §5.5 三档 40/30/30 口径）
    """
    if per_share_risk <= 0:
        return 0
    first_risk_amount = first_unit.per_share_risk * first_unit.股数
    budget = first_risk_amount * ADD_UNIT_RISK_RATIO
    return int(budget // per_share_risk // 100) * 100
