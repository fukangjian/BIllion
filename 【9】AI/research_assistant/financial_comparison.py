"""
财报对比助手 — 多家公司财务数据对比与 LLM 差异分析
"""
import argparse
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import FINANCIAL_OUTPUT_DIR
from llm_client import call_llm, has_llm_api_key
from prompts import FINANCIAL_COMPARISON, FINANCIAL_COMPARISON_SYSTEM
from utils import (
    df_to_markdown_table,
    get_stock_name,
    normalize_symbol,
    obsidian_frontmatter,
    safe_fetch,
    write_markdown,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 关注的财务指标列（AkShare 返回列名可能不同，做模糊匹配）
KEY_METRICS = [
    "营业收入",
    "净利润",
    "净资产收益率",
    "ROE",
    "毛利率",
    "净利率",
    "资产负债率",
    "经营活动产生的现金流量净额",
    "研发支出",
    "研发投入",
    "每股收益",
    "营业总收入",
    "归母净利润",
]


def fetch_financial_indicators(symbol: str) -> pd.DataFrame:
    """获取个股财务分析指标"""
    import akshare as ak

    symbol = normalize_symbol(symbol)
    df = safe_fetch(
        ak.stock_financial_analysis_indicator,
        symbol=symbol,
        start_year="2020",
        default=pd.DataFrame(),
    )
    return df if df is not None else pd.DataFrame()


def fetch_financial_abstract(symbol: str) -> pd.DataFrame:
    """获取财务摘要（备用数据源）"""
    import akshare as ak

    symbol = normalize_symbol(symbol)
    df = safe_fetch(
        ak.stock_financial_abstract,
        symbol=symbol,
        default=pd.DataFrame(),
    )
    return df if df is not None else pd.DataFrame()


def _extract_latest_metrics(df: pd.DataFrame, symbol: str, stock_name: str) -> dict:
    """从财务数据中提取最新一期关键指标"""
    result = {"代码": symbol, "名称": stock_name}

    if df.empty:
        return result

    # 财务分析指标格式：行是指标，列是日期
    if "指标" in df.columns or df.index.name == "指标":
        metrics_df = df.set_index("指标") if "指标" in df.columns else df
        date_cols = [c for c in metrics_df.columns if str(c).startswith("20")]
        if date_cols:
            latest_col = sorted(date_cols)[-1]
            for metric in KEY_METRICS:
                for idx in metrics_df.index:
                    if metric in str(idx):
                        val = metrics_df.loc[idx, latest_col]
                        result[str(idx)] = val
                        break
        return result

    # 财务摘要格式
    for col in df.columns:
        for metric in KEY_METRICS:
            if metric in str(col):
                if len(df) > 0:
                    result[str(col)] = df.iloc[-1][col]

    return result


def build_comparison_table(symbols: list[str]) -> pd.DataFrame:
    """构建多家公司财务对比表"""
    rows = []
    for sym in symbols:
        sym = normalize_symbol(sym)
        name = get_stock_name(sym)
        df = fetch_financial_indicators(sym)
        if df.empty:
            df = fetch_financial_abstract(sym)
        metrics = _extract_latest_metrics(df, sym, name)
        rows.append(metrics)

    if not rows:
        return pd.DataFrame()

    comparison = pd.DataFrame(rows)
    # 代码和名称放前面
    cols = ["代码", "名称"] + [c for c in comparison.columns if c not in ("代码", "名称")]
    return comparison[[c for c in cols if c in comparison.columns]]


def analyze_comparison(table_md: str, symbols: list[str]) -> str:
    """LLM 差异分析"""
    if not has_llm_api_key():
        return "_需配置 LLM API 密钥后生成差异分析。上方表格为原始财务数据。_"

    prompt = FINANCIAL_COMPARISON.format(
        symbols=", ".join(symbols),
        table=table_md,
    )
    result = call_llm(prompt, system_prompt=FINANCIAL_COMPARISON_SYSTEM)
    return result or "_LLM 分析生成失败，请查看上方数据表格。_"


def generate_report(
    symbols: list[str],
    output_dir: Path | None = None,
) -> Path:
    """生成财报对比 Markdown 报告"""
    symbols = [normalize_symbol(s) for s in symbols]
    output_dir = output_dir or FINANCIAL_OUTPUT_DIR
    today = datetime.now().strftime("%Y-%m-%d")

    comparison_df = build_comparison_table(symbols)
    table_md = df_to_markdown_table(comparison_df) if not comparison_df.empty else "_未能获取财务数据_"

    lines = [
        obsidian_frontmatter(
            ["AI研究", "财报对比"],
            symbols=",".join(symbols),
            date=today,
        ),
        f"# 财报对比分析",
        "",
        f"> 对比公司: {', '.join(f'{get_stock_name(s)}({s})' for s in symbols)}",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 财务数据对比",
        "",
        table_md,
        "",
        "---",
        "",
        "## AI 差异分析",
        "",
    ]

    analysis = analyze_comparison(table_md, symbols)
    lines.append(analysis)

    if not has_llm_api_key():
        lines.append("")
        lines.append("> ⚠️ 未配置 LLM API 密钥，仅输出原始数据表格。")

    code_str = "_".join(symbols[:3])
    output_path = output_dir / f"{today}_财报对比_{code_str}.md"
    return write_markdown("\n".join(lines), output_path)


def main():
    parser = argparse.ArgumentParser(description="财报对比助手")
    parser.add_argument("symbols", nargs="+", help="股票代码列表，如 600519 000858")
    parser.add_argument("-o", "--output", type=str, default=None, help="输出目录")
    args = parser.parse_args()

    output_dir = Path(args.output) if args.output else None
    path = generate_report(args.symbols, output_dir=output_dir)
    print(f"报告已生成: {path}")


if __name__ == "__main__":
    main()
