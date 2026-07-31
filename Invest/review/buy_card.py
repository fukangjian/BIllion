"""
买入卡自动生成 — 建仓成功后按【10】实盘记录/模板/买入卡模板.md 栏目生成 MD

输出到 config.TRADE_LOG_OUTPUT_DIR（vault 的 交易日志/ 目录），
文件名 {交易编号}_{股票名称}_买入卡.md。生成失败仅打印警告，不影响建仓主流程。
"""
import logging
from pathlib import Path

from config import ADD_SPACING_MAX, ADD_SPACING_MIN, EXIT_CHANNEL_PERIODS, TRADE_LOG_OUTPUT_DIR
from review.trade_log import Trade
from shared.utils import obsidian_frontmatter, write_markdown

logger = logging.getLogger(__name__)

# 入场系统 → 模板「策略类型」栏目措辞
_STRATEGY_LABELS = {
    "S1-A": "S1-A 20日突破",
    "S2-A": "S2-A 55日突破",
}

_TBD = "（待填写）"


def _strategy_label(entry_system: str) -> str:
    return _STRATEGY_LABELS.get(entry_system, entry_system or _TBD)


def _exit_channel_period(entry_system: str) -> int | None:
    """按入场系统关键字匹配退出通道周期（与 review/monitor.py 口径一致）"""
    s = (entry_system or "").upper()
    for key, period in EXIT_CHANNEL_PERIODS.items():
        if key in s:
            return period
    return None


def _fmt_money(v: float) -> str:
    return f"{v:,.0f}"


def calc_dict_from_trade(trade: Trade, equity: float) -> dict:
    """
    从实际建仓参数构造仓位计算字典（与 position_calculator.calc_position 返回同构）。
    供 cmd_add 生成买入卡使用：以用户实际成交的风险率/股数为准，N 取每股风险。
    """
    per_share = trade.per_share_risk
    return {
        "账户权益": equity,
        "风险率": trade.风险率,
        "风险预算": equity * trade.风险率 / 100,
        "每股风险": per_share,
        "股数": trade.股数,
        "仓位金额": trade.仓位金额,
        "仓位比例": trade.仓位金额 / equity * 100 if equity > 0 else 0.0,
        "加仓价1": round(trade.入场价 + ADD_SPACING_MIN * per_share, 2) if per_share > 0 else None,
        "加仓价2": round(trade.入场价 + ADD_SPACING_MAX * per_share, 2) if per_share > 0 else None,
    }


def build_buy_card_content(trade: Trade, calc: dict, scan_info: dict | None = None) -> str:
    """
    按买入卡模板栏目生成 Markdown 正文（含 frontmatter）。

    calc 为 position_calculator.calc_position 的返回 dict（或同构字典）。
    scan_info 可选，来自扫描 JSON 的突破条目（channel_high/atr_20/breakout_pct/period）。
    """
    symbol = trade.股票代码
    name = trade.股票名称 or symbol
    stop = trade.止损价
    exit_period = _exit_channel_period(trade.入场系统)

    # --- 价格与信号（有扫描数据则自动填，否则待填写） ---
    high20, high55, atr_text = _TBD, _TBD, _TBD
    trigger_price = _TBD
    stop_text = f"{stop:.2f} 元"
    if scan_info:
        channel_high = scan_info.get("channel_high")
        period = scan_info.get("period")
        if channel_high:
            trigger_price = f"{channel_high:.2f}"
            if period == 20:
                high20 = f"{channel_high:.2f}"
            elif period == 55:
                high55 = f"{channel_high:.2f}"
        if scan_info.get("atr_20"):
            atr_text = f"{scan_info['atr_20']:.2f}"
            stop_text = f"{stop:.2f} 元（入场价 − 2×ATR）"

    # --- 失效条件（能自动填的全填） ---
    invalidation = [f"收盘价跌破初始止损 {stop:.2f} 元"]
    if exit_period:
        invalidation.append(f"收盘价跌破 {exit_period} 日退出通道下轨（系统退出）")

    max_loss = calc.get("风险预算", 0.0)

    frontmatter = obsidian_frontmatter(
        tags=["买入卡", symbol, trade.入场系统 or "未知策略"]
    )

    return f"""{frontmatter}
# 买入卡

> 使用方法：下单前填写完毕，下单后不得修改计划区内容。链接至 [[投资体系_V5.0]] 和 [[行业选择标准]]。

## 基本信息

| 字段 | 内容 |
|---|---|
| **股票** | {name} |
| **代码** | {symbol} |
| **账户类型** | {trade.账户类型 or _TBD} |
| **风险簇** | {trade.风险簇 or _TBD} |
| **策略类型** | {_strategy_label(trade.入场系统)} |
| **计划日期** | {trade.日期} |

## 投资逻辑

**一句话逻辑**：

> {_TBD}

**未来 3—12 个月催化**：

1. {_TBD}
2. 
3. 

**一手资料链接**：

- 公告/财报：{_TBD}
- 政策/行业：
- 其他：

## 证据链

### 三个支持证据

1. {_TBD}
2. 
3. 

### 三个反对证据

1. {_TBD}
2. 
3. 

## 价格与信号

| 字段 | 数值 |
|---|---:|
| **20 日最高价** | {high20} |
| **55 日最高价** | {high55} |
| **ATR(20)** | {atr_text} |
| **入场触发** | 价格 ≥ {trigger_price} 且成交量 ≥ 20 日均量 × {_TBD} |
| **实际入场** | （成交后填写） |
| **初始止损** | {stop_text} |
| **10 日退出线** | {_TBD} |
| **20 日退出线** | {_TBD} |

## 仓位计算

| 字段 | 数值 |
|---|---:|
| **账户权益** | {_fmt_money(calc.get('账户权益', 0))} 元 |
| **单笔风险率** | {calc.get('风险率', 0)}% |
| **风险预算 R** | {_fmt_money(calc.get('风险预算', 0))} 元 |
| **每股风险** | {calc.get('每股风险', 0):.2f} 元（含跳空缓冲） |
| **首仓股数** | {calc.get('股数', 0)} 股 |
| **名义仓位** | {_fmt_money(calc.get('仓位金额', 0))} 元 |
| **账户占比** | {calc.get('仓位比例', 0):.2f}% |
| **加仓价 1** | {calc.get('加仓价1') if calc.get('加仓价1') is not None else _TBD}（首仓 + 0.5N） |
| **加仓价 2** | {calc.get('加仓价2') if calc.get('加仓价2') is not None else _TBD}（首仓 + 1N） |

## 失效与极端情景

**逻辑失效条件**：

{chr(10).join(f'- {x}' for x in invalidation)}

**最坏跳空情景**（估算最大损失）：

- 情景描述：开盘跳空跌破止损价 {stop:.2f} 元
- 预估损失：约 1 R / {_fmt_money(max_loss)} 元

## 下单前确认

- [ ] 已通过生存闸门（系统 0）
- [ ] 市场状态允许该策略（A/B/C/D）
- [ ] 风险簇未超限
- [ ] 账户热度未超限
- [ ] 回撤状态允许新开仓
- [ ] 兴奋度 + FOMO ≤ 6/10
- [ ] 接受止损结果

---

**关联文件**：[[持仓卡模板]] · [[复盘卡模板]]
"""


def generate_buy_card(
    trade: Trade,
    calc: dict,
    scan_info: dict | None = None,
    output_dir: Path | None = None,
) -> Path | None:
    """
    生成买入卡 MD 文件，返回路径；失败（如 vault 路径不可写）打印警告并返回 None。
    """
    try:
        out = output_dir or TRADE_LOG_OUTPUT_DIR
        name = trade.股票名称 or trade.股票代码
        path = out / f"{trade.交易编号}_{name}_买入卡.md"
        content = build_buy_card_content(trade, calc, scan_info=scan_info)
        return write_markdown(content, path)
    except Exception as e:
        logger.warning("买入卡生成失败（不影响建仓）: %s", e)
        print(f"[警告] 买入卡生成失败（不影响建仓）: {e}")
        return None
