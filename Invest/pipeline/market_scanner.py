"""
每日市场扫描 — 突破候选、板块强度、市场状态，输出 Markdown 报告
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
from datetime import datetime

import pandas as pd

from config import (
    CHANNEL_LONG,
    CHANNEL_SHORT,
    DEFAULT_INDEX_SYMBOL,
    MARKET_STATE,
    MARKET_SCAN_OUTPUT_DIR,
    WATCHLIST,
)
from pipeline.database import (
    DB_PATH,
    get_connection,
    init_database,
    load_daily_quotes,
    load_sector_quotes,
    save_market_state,
)
from pipeline.indicators import (
    judge_market_state,
    rank_sectors_by_strength,
    scan_breakout_candidates,
)

logger = logging.getLogger(__name__)


def _load_limit_stats_from_db(db_path=None) -> pd.DataFrame:
    path = db_path or DB_PATH
    try:
        with get_connection(path) as conn:
            return pd.read_sql_query(
                "SELECT * FROM limit_stats ORDER BY trade_date DESC LIMIT 5",
                conn,
            )
    except Exception:
        return pd.DataFrame()


def run_scan(
    symbols: list[str] | None = None,
    output_dir: Path | None = None,
) -> Path:
    """执行完整市场扫描，生成 Markdown 报告"""
    init_database()
    symbols = symbols or WATCHLIST
    output_dir = output_dir or MARKET_SCAN_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    report_path = output_dir / f"market_scan_{today}.md"

    symbols_data = {}
    for sym in symbols:
        df = load_daily_quotes(symbol=sym)
        if not df.empty:
            symbols_data[sym] = df

    hs300_df = load_daily_quotes(symbol=DEFAULT_INDEX_SYMBOL)
    if hs300_df.empty:
        hs300_df = load_daily_quotes(symbol="000300")

    limit_stats = _load_limit_stats_from_db()
    breakout_20 = scan_breakout_candidates(symbols_data, CHANNEL_SHORT)
    breakout_55 = scan_breakout_candidates(symbols_data, CHANNEL_LONG)
    sector_rank = _build_sector_ranking(hs300_df)

    state_info = judge_market_state(
        hs300_df,
        limit_stats,
        ma_weeks=MARKET_STATE["ma_weeks"],
        volume_median_days=MARKET_STATE["volume_median_days"],
        breadth_bull=MARKET_STATE["breadth_bull"],
        breadth_bear=MARKET_STATE["breadth_bear"],
    )

    if hs300_df is not None and not hs300_df.empty:
        save_market_state({
            "trade_date": today,
            "state": state_info["state"],
            "hs300_close": state_info["hs300_close"],
            "hs300_ma20w": state_info["hs300_ma20w"],
            "volume_ratio": state_info["volume_ratio"],
            "breadth_ratio": state_info["breadth_ratio"],
            "suggested_pos": state_info["suggested_pos"],
            "notes": "; ".join(state_info["notes"]),
        })

    md = _format_report(today, state_info, breakout_20, breakout_55, sector_rank, symbols)
    report_path.write_text(md, encoding="utf-8")
    logger.info("扫描报告已生成: %s", report_path)
    return report_path


def _build_sector_ranking(benchmark_df: pd.DataFrame) -> pd.DataFrame:
    try:
        with get_connection(DB_PATH) as conn:
            sectors = pd.read_sql_query(
                "SELECT DISTINCT sector_name FROM sector_quotes",
                conn,
            )
    except Exception:
        return pd.DataFrame()

    sector_data = {}
    for name in sectors["sector_name"].tolist():
        df = load_sector_quotes(sector_name=name)
        if not df.empty:
            sector_data[name] = df

    if not sector_data or benchmark_df.empty:
        return pd.DataFrame()

    return rank_sectors_by_strength(sector_data, benchmark_df)


def _format_report(
    date: str,
    state_info: dict,
    breakout_20: pd.DataFrame,
    breakout_55: pd.DataFrame,
    sector_rank: pd.DataFrame,
    symbols: list[str],
) -> str:
    lines = [
        "# 每日市场扫描报告",
        "",
        f"> 生成时间: {date}",
        f"> 扫描股票数: {len(symbols)}",
        "",
        "---",
        "",
        "## 一、市场状态判断",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| **市场状态** | **{state_info['state']}** |",
        f"| 建议仓位 | {state_info['suggested_pos']} |",
        f"| 沪深300收盘 | {state_info.get('hs300_close', 'N/A')} |",
        f"| 20周均线 | {state_info.get('hs300_ma20w', 'N/A')} |",
        f"| 成交额/中位数 | {state_info.get('volume_ratio', 1.0):.2f} |",
        f"| 上涨/下跌比 | {state_info.get('breadth_ratio', 1.0):.2f} |",
        "",
        f"**判断依据**: {'; '.join(state_info.get('notes', []))}",
        "",
        "### 状态说明",
        "",
        "- **A** (70%-90%): 指数、成交、主线共振",
        "- **B** (40%-70%): 结构性行情",
        "- **C** (20%-50%): 震荡轮动",
        "- **D** (0%-30%): 系统性下跌",
        "",
        "---",
        "",
        "## 二、20日通道突破候选 (S1-A)",
        "",
    ]

    if breakout_20.empty:
        lines.append("_暂无突破候选_")
    else:
        lines.append("| 代码 | 收盘价 | 通道高点 | 突破幅度% | ATR(20) |")
        lines.append("|------|--------|----------|-----------|---------|")
        for _, r in breakout_20.iterrows():
            lines.append(
                f"| {r['symbol']} | {r['close']:.2f} | {r['channel_high']:.2f} "
                f"| {r['breakout_pct']:.2f} | {r.get('atr_20', 0):.2f} |"
            )

    lines.extend(["", "---", "", "## 三、55日通道突破候选 (S2-A)", ""])

    if breakout_55.empty:
        lines.append("_暂无突破候选_")
    else:
        lines.append("| 代码 | 收盘价 | 通道高点 | 突破幅度% | ATR(20) |")
        lines.append("|------|--------|----------|-----------|---------|")
        for _, r in breakout_55.iterrows():
            lines.append(
                f"| {r['symbol']} | {r['close']:.2f} | {r['channel_high']:.2f} "
                f"| {r['breakout_pct']:.2f} | {r.get('atr_20', 0):.2f} |"
            )

    lines.extend(["", "---", "", "## 四、板块相对强度排名 (Top 15)", ""])

    if sector_rank.empty:
        lines.append("_暂无板块数据，请先运行数据获取_")
    else:
        lines.append("| 排名 | 板块 | 相对强度 | 20日涨幅 |")
        lines.append("|------|------|----------|----------|")
        for _, r in sector_rank.head(15).iterrows():
            rs = r.get("relative_strength", 0)
            rs_str = f"{rs:.2f}" if pd.notna(rs) else "N/A"
            ret = r.get("period_return", 0)
            ret_str = f"{ret*100:.1f}%" if pd.notna(ret) else "N/A"
            lines.append(f"| {r['rank']} | {r['sector_name']} | {rs_str} | {ret_str} |")

    lines.extend([
        "",
        "---",
        "",
        "## 五、使用说明",
        "",
        "1. 20日突破对应 **S1-A 快速系统**，10日通道退出",
        "2. 55日突破对应 **S2-A 慢速系统**，20日通道退出",
        "3. 入场前请结合板块强度和市场状态调整仓位",
        "4. 使用 `position_calculator.py` 计算具体股数",
        "",
        "---",
        "_本报告由量化系统自动生成，仅供参考，不构成投资建议_",
    ])

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    path = run_scan()
    print(f"报告路径: {path}")
