"""
每日研究日报 — 综合公告、板块、宏观数据、突破候选
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import argparse
import logging
from datetime import datetime

import pandas as pd

from config import (
    DEFAULT_SECTORS,
    RESEARCH_WATCHLIST,
    DAILY_REPORT_OUTPUT_DIR,
    MARKET_SCANNER_OUTPUT,
)
from review.positions import get_position_for_watchlist
from shared.llm_client import call_llm, has_llm_api_key
from shared.prompts import DAILY_REPORT_SUMMARY
from shared.utils import (
    df_to_markdown_table,
    get_stock_name,
    normalize_symbol,
    obsidian_frontmatter,
    safe_fetch,
    write_markdown,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def fetch_market_overview() -> dict:
    import akshare as ak

    result = {
        "indices": [],
        "turnover": None,
        "up_count": None,
        "down_count": None,
        "limit_up": None,
        "limit_down": None,
    }

    index_map = {
        "000001": "上证指数",
        "399001": "深证成指",
        "399006": "创业板指",
        "000300": "沪深300",
    }
    try:
        spot = safe_fetch(ak.stock_zh_index_spot_em, default=pd.DataFrame())
        if spot is not None and not spot.empty:
            for code, name in index_map.items():
                row = spot[spot["代码"] == code]
                if not row.empty:
                    r = row.iloc[0]
                    result["indices"].append({
                        "名称": name,
                        "最新价": r.get("最新价", "-"),
                        "涨跌幅": f"{r.get('涨跌幅', '-')}%",
                        "成交额": r.get("成交额", "-"),
                    })
    except Exception as e:
        logger.warning("获取指数数据失败: %s", e)

    try:
        activity = ak.stock_market_activity_legu()
        if activity is not None and not activity.empty:
            latest = activity.iloc[-1]
            for col in activity.columns:
                col_str = str(col)
                if "上涨" in col_str:
                    result["up_count"] = latest[col]
                elif "下跌" in col_str:
                    result["down_count"] = latest[col]
                elif "涨停" in col_str:
                    result["limit_up"] = latest[col]
                elif "跌停" in col_str:
                    result["limit_down"] = latest[col]
    except Exception:
        logger.debug("市场活跃度数据源(乐股)暂不可用，跳过")

    return result


def fetch_sector_performance(sectors: list[str]) -> pd.DataFrame:
    import akshare as ak

    try:
        df = safe_fetch(ak.stock_board_industry_name_em, default=pd.DataFrame())
        if df is None or df.empty:
            return pd.DataFrame()

        rows = []
        for sector in sectors:
            matched = df[df["板块名称"].str.contains(sector[:2], na=False)]
            if not matched.empty:
                for _, r in matched.head(2).iterrows():
                    rows.append({
                        "板块": r.get("板块名称", sector),
                        "涨跌幅(%)": r.get("涨跌幅", "-"),
                        "成交额": r.get("成交额", "-"),
                        "领涨股": r.get("领涨股票", "-"),
                    })
        return pd.DataFrame(rows)
    except Exception as e:
        logger.warning("获取板块数据失败: %s", e)
        return pd.DataFrame()


def fetch_announcement_summary(symbol: str) -> str:
    from research.announcement_analyzer import fetch_announcements

    symbol = normalize_symbol(symbol)
    name = get_stock_name(symbol)
    df = fetch_announcements(symbol, limit=3)
    if df.empty:
        return f"- **{name}({symbol})**: 无近期公告"

    items = []
    for _, row in df.iterrows():
        title = str(row.get("title", ""))[:40]
        date = str(row.get("date", ""))[:10]
        items.append(f"{date} {title}")
    return f"- **{name}({symbol})**: " + " | ".join(items)


def load_breakout_candidates() -> str:
    """尝试加载市场扫描结果"""
    today = datetime.now().strftime("%Y-%m-%d")
    scan_file = MARKET_SCANNER_OUTPUT / f"market_scan_{today}.md"

    if scan_file.exists():
        content = scan_file.read_text(encoding="utf-8")
        if "突破候选" in content or "breakout" in content.lower():
            lines = content.split("\n")
            start = None
            extracted = []
            for i, line in enumerate(lines):
                if "突破" in line and start is None:
                    start = i
                if start is not None:
                    extracted.append(line)
                    if len(extracted) > 30:
                        break
            if extracted:
                return "\n".join(extracted)

    try:
        from pipeline.market_scanner import run_scan

        report_path = run_scan()
        if report_path.exists():
            return f"已生成最新扫描报告: [[{report_path.name}]]\n\n" + report_path.read_text(encoding="utf-8")[:2000]
    except Exception as e:
        logger.debug("market_scanner 联动失败: %s", e)

    return "_市场扫描未运行或无数据。可手动执行: `cd Invest && python pipeline/run_daily.py` 或 `python run_all.py`_"


def generate_executive_summary(materials: str) -> str:
    if not has_llm_api_key():
        return "_配置 LLM API 密钥后可自动生成今日要点摘要。_"

    prompt = DAILY_REPORT_SUMMARY.format(materials=materials[:4000])
    result = call_llm(prompt, temperature=0.2, max_tokens=500)
    return result or "_摘要生成失败_"


def resolve_watchlist(watchlist: list[str] | None = None) -> list[str]:
    """解析关注列表：CLI 覆盖 > 持仓 + 研究默认列表"""
    if watchlist:
        return watchlist
    merged = list(RESEARCH_WATCHLIST)
    for sym in get_position_for_watchlist():
        if sym not in merged:
            merged.append(sym)
    return merged


def generate_report(
    watchlist: list[str] | None = None,
    sectors: list[str] | None = None,
    output_dir: Path | None = None,
) -> Path:
    watchlist = resolve_watchlist(watchlist)
    sectors = sectors or DEFAULT_SECTORS
    output_dir = output_dir or DAILY_REPORT_OUTPUT_DIR
    today = datetime.now().strftime("%Y-%m-%d")

    market = fetch_market_overview()
    sector_df = fetch_sector_performance(sectors)
    breakout = load_breakout_candidates()

    announcement_lines = []
    for sym in watchlist:
        try:
            announcement_lines.append(fetch_announcement_summary(sym))
        except Exception as e:
            logger.warning("获取 %s 公告失败: %s", sym, e)
            announcement_lines.append(f"- **{sym}**: 获取失败")

    lines = [
        obsidian_frontmatter(["AI研究", "每日日报"], date=today),
        f"# 每日研究日报 · {today}",
        "",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    materials_parts = []
    if market["indices"]:
        materials_parts.append("指数: " + str(market["indices"][:2]))
    if not sector_df.empty:
        materials_parts.append("板块: " + sector_df.head(3).to_string())
    materials_parts.extend(announcement_lines[:3])

    summary = generate_executive_summary("\n".join(materials_parts))
    lines.extend(["## 今日要点", "", summary, ""])

    lines.extend(["## 市场宏观", ""])
    if market["indices"]:
        idx_df = pd.DataFrame(market["indices"])
        lines.append(df_to_markdown_table(idx_df))
    else:
        lines.append("_指数数据获取失败_")

    lines.append("")
    breadth = []
    if market["up_count"] is not None:
        breadth.append(f"上涨: {market['up_count']}")
    if market["down_count"] is not None:
        breadth.append(f"下跌: {market['down_count']}")
    if market["limit_up"] is not None:
        breadth.append(f"涨停: {market['limit_up']}")
    if market["limit_down"] is not None:
        breadth.append(f"跌停: {market['limit_down']}")
    if breadth:
        lines.append(f"**市场宽度**: {' · '.join(breadth)}")
    lines.append("")

    lines.extend(["## 关注板块表现", ""])
    if not sector_df.empty:
        lines.append(df_to_markdown_table(sector_df))
    else:
        lines.append("_板块数据获取失败_")
    lines.append("")

    lines.extend(["## 持仓/关注公司公告", ""])
    lines.extend(announcement_lines)
    lines.append("")

    lines.extend(["## 突破候选提示", ""])
    lines.append(breakout)
    lines.append("")

    lines.extend([
        "---",
        "",
        "## 相关工具",
        "",
        "- 公告深度分析: `python research/announcement_analyzer.py <代码>`",
        "- 财报对比: `python research/financial_comparison.py <代码1> <代码2>`",
        "- 产业链映射: `python research/industry_mapper.py <关键词>`",
        "",
    ])

    output_path = output_dir / f"{today}_每日研究日报.md"
    return write_markdown("\n".join(lines), output_path)


def main():
    parser = argparse.ArgumentParser(description="每日研究日报")
    parser.add_argument("-w", "--watchlist", nargs="*", default=None, help="关注股票代码列表")
    parser.add_argument("-s", "--sectors", nargs="*", default=None, help="关注板块关键词")
    parser.add_argument("-o", "--output", type=str, default=None, help="输出目录")
    args = parser.parse_args()

    output_dir = Path(args.output) if args.output else None
    path = generate_report(
        watchlist=args.watchlist,
        sectors=args.sectors,
        output_dir=output_dir,
    )
    print(f"日报已生成: {path}")


if __name__ == "__main__":
    main()
