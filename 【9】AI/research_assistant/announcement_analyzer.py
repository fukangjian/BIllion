"""
公告摘要助手 — 获取上市公司最新公告并用 LLM 生成结构化摘要
"""
import argparse
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import ANNOUNCEMENT_LIMIT, ANNOUNCEMENT_OUTPUT_DIR
from llm_client import call_llm, has_llm_api_key
from prompts import ANNOUNCEMENT_ANALYSIS, ANNOUNCEMENT_SYSTEM
from utils import (
    get_stock_name,
    normalize_symbol,
    obsidian_frontmatter,
    safe_fetch,
    write_markdown,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def fetch_announcements(symbol: str, limit: int = ANNOUNCEMENT_LIMIT) -> pd.DataFrame:
    """使用 AkShare 获取最新公告列表"""
    import akshare as ak

    symbol = normalize_symbol(symbol)
    df = safe_fetch(
        ak.stock_notice_report,
        symbol=symbol,
        default=pd.DataFrame(),
    )
    if df is None or df.empty:
        logger.warning("股票 %s 未获取到公告", symbol)
        return pd.DataFrame()

    # 统一列名
    col_map = {}
    for col in df.columns:
        lower = str(col).lower()
        if "公告" in col and "标题" in col:
            col_map[col] = "title"
        elif "公告" in col and "时间" in col:
            col_map[col] = "date"
        elif "公告" in col and "类型" in col:
            col_map[col] = "category"
        elif "网址" in col or "url" in lower or "链接" in col:
            col_map[col] = "url"

    df = df.rename(columns=col_map)
    if "date" in df.columns:
        df = df.sort_values("date", ascending=False)
    return df.head(limit)


def fetch_announcement_content(url: str) -> str:
    """尝试获取公告正文（简化版：仅返回 URL 提示）"""
    if not url or pd.isna(url):
        return "（公告正文需手动查阅巨潮资讯网）"
    return f"公告链接: {url}\n（完整正文请访问上述链接）"


def analyze_announcement(
    symbol: str,
    title: str,
    date: str,
    content: str,
    stock_name: str = "",
) -> str:
    """调用 LLM 分析单条公告"""
    if not has_llm_api_key():
        return _fallback_analysis(title, date)

    prompt = ANNOUNCEMENT_ANALYSIS.format(
        symbol=symbol,
        stock_name=stock_name or symbol,
        title=title,
        date=date,
        content=content[:3000],  # 限制长度
    )
    result = call_llm(prompt, system_prompt=ANNOUNCEMENT_SYSTEM)
    return result or _fallback_analysis(title, date)


def _fallback_analysis(title: str, date: str) -> str:
    """无 LLM 时的降级输出"""
    return f"""## 公告类型
待人工分类（标题: {title}）

## 关键数字
_需配置 LLM API 密钥后自动提取，或手动查阅公告_

## 投资逻辑影响
中性 — 待分析

## 需要关注的风险点
- 请查阅完整公告原文确认细节

## 后续跟踪要点
- 公告日期: {date}
"""


def generate_report(
    symbol: str,
    limit: int = ANNOUNCEMENT_LIMIT,
    output_dir: Path | None = None,
) -> Path:
    """生成公告摘要 Markdown 报告"""
    symbol = normalize_symbol(symbol)
    stock_name = get_stock_name(symbol)
    output_dir = output_dir or ANNOUNCEMENT_OUTPUT_DIR
    today = datetime.now().strftime("%Y-%m-%d")

    announcements = fetch_announcements(symbol, limit)
    lines = [
        obsidian_frontmatter(
            ["AI研究", "公告摘要"],
            symbol=symbol,
            stock_name=stock_name,
            date=today,
        ),
        f"# {stock_name} ({symbol}) 公告摘要",
        "",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    if announcements.empty:
        lines.append("_未获取到近期公告，请检查股票代码或网络连接。_")
    else:
        lines.append(f"## 公告列表（最近 {len(announcements)} 条）")
        lines.append("")
        list_rows = ["| 日期 | 标题 | 类型 |", "| --- | --- | --- |"]
        for _, row in announcements.iterrows():
            title = str(row.get("title", ""))[:60]
            date = str(row.get("date", ""))[:10]
            cat = str(row.get("category", "-"))
            list_rows.append(f"| {date} | {title} | {cat} |")
        lines.extend(list_rows)
        lines.append("")

        # 分析最新一条公告
        latest = announcements.iloc[0]
        title = str(latest.get("title", ""))
        date = str(latest.get("date", ""))[:10]
        url = str(latest.get("url", ""))
        content = fetch_announcement_content(url)

        lines.append("---")
        lines.append("")
        lines.append(f"## 最新公告深度分析: {title}")
        lines.append("")
        analysis = analyze_announcement(symbol, title, date, content, stock_name)
        lines.append(analysis)

        if not has_llm_api_key():
            lines.append("")
            lines.append("> ⚠️ 未配置 OPENAI_API_KEY 或 ANTHROPIC_API_KEY，以上为降级输出。")

    output_path = output_dir / f"{today}_{symbol}_{stock_name}_公告摘要.md"
    return write_markdown("\n".join(lines), output_path)


def main():
    parser = argparse.ArgumentParser(description="公告摘要助手")
    parser.add_argument("symbol", help="股票代码，如 688192")
    parser.add_argument("-n", "--limit", type=int, default=ANNOUNCEMENT_LIMIT, help="公告数量上限")
    parser.add_argument("-o", "--output", type=str, default=None, help="输出目录")
    args = parser.parse_args()

    output_dir = Path(args.output) if args.output else None
    path = generate_report(args.symbol, limit=args.limit, output_dir=output_dir)
    print(f"报告已生成: {path}")


if __name__ == "__main__":
    main()
