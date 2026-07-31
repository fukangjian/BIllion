"""
公告摘要助手 — 获取上市公司最新公告并用 LLM 生成结构化摘要
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

from config import ANNOUNCEMENT_LIMIT, ANNOUNCEMENT_MAX_CHARS, ANNOUNCEMENT_OUTPUT_DIR
from research.announcement_fetcher import (
    fetch_announcement_full_text,
    fetch_latest_announcements,
    has_real_content,
    summarize_long_announcement,
)
from shared.llm_client import call_llm, has_llm_api_key
from shared.prompts import ANNOUNCEMENT_ANALYSIS, ANNOUNCEMENT_SYSTEM
from shared.utils import (
    get_stock_name,
    normalize_symbol,
    obsidian_frontmatter,
    write_markdown,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def fetch_announcements(symbol: str, limit: int = ANNOUNCEMENT_LIMIT) -> pd.DataFrame:
    """获取最新公告列表（列: title/date/category/url，按日期倒序）。

    fallback 链（东财当日公告 → 巨潮 HTTP API 历史公告）在 fetcher 内实现。
    """
    symbol = normalize_symbol(symbol)
    try:
        df = fetch_latest_announcements(symbol, limit=limit)
    except Exception as e:
        logger.error("获取 %s 公告失败: %s", symbol, e)
        return pd.DataFrame()
    if df.empty:
        logger.warning("股票 %s 未获取到公告", symbol)
    return df


def fetch_announcement_content(
    url: str,
    symbol: str = "",
    title: str = "",
    date: str = "",
) -> str:
    """获取公告全文：巨潮 HTML 解析 / PDF 标注 / 本地缓存"""
    if not url and not (symbol and title):
        return "（公告正文需手动查阅巨潮资讯网）"

    raw_content = fetch_announcement_full_text(url, symbol=symbol, title=title, date=date)

    # PDF 或解析失败时直接返回
    if "PDF 公告需手动查看" in raw_content or "解析失败" in raw_content:
        return raw_content
    if not raw_content or len(raw_content) < 50:
        link = url or "巨潮资讯网"
        return f"公告链接: {link}\n（完整正文请访问上述链接）"

    # 长公告分块摘要后截断
    if len(raw_content) > ANNOUNCEMENT_MAX_CHARS:
        return summarize_long_announcement(
            raw_content, symbol=symbol, title=title, max_chars=ANNOUNCEMENT_MAX_CHARS
        )
    return raw_content[:ANNOUNCEMENT_MAX_CHARS]


def analyze_announcement(
    symbol: str,
    title: str,
    date: str,
    content: str,
    stock_name: str = "",
) -> str:
    # 防编造护栏：未取得真实正文时绝不调用 LLM，避免凭空编造数字
    if not has_real_content(content):
        return "> ⚠️ 未取得公告正文，不做解读。请通过公告链接查阅原文。"

    if not has_llm_api_key():
        return _fallback_analysis(title, date)

    prompt = ANNOUNCEMENT_ANALYSIS.format(
        symbol=symbol,
        stock_name=stock_name or symbol,
        title=title,
        date=date,
        content=content[:ANNOUNCEMENT_MAX_CHARS],
    )
    result = call_llm(
        prompt,
        system_prompt=ANNOUNCEMENT_SYSTEM,
        task_type="announcement",
    )
    return result or _fallback_analysis(title, date)


def _fallback_analysis(title: str, date: str) -> str:
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

        # 新鲜度检查：最新公告距今 >30 天时显式标注
        latest_date_str = str(announcements.iloc[0].get("date", ""))[:10]
        try:
            age_days = (datetime.now() - datetime.strptime(latest_date_str, "%Y-%m-%d")).days
        except ValueError:
            age_days = -1
        if age_days > 30:
            lines.append(f"> ⚠️ 公告数据可能陈旧（最新：{latest_date_str}，距今 {age_days} 天）")
            lines.append("")

        latest = announcements.iloc[0]
        title = str(latest.get("title", ""))
        date = str(latest.get("date", ""))[:10]
        url = str(latest.get("url", ""))
        content = fetch_announcement_content(url, symbol=symbol, title=title, date=date)

        lines.append("---")
        lines.append("")
        lines.append(f"## 最新公告: {title}")
        lines.append("")
        lines.append(f"- 日期: {date}")
        lines.append(f"- 链接: {url or '巨潮资讯网'}")
        lines.append("")

        if not has_real_content(content):
            # 防编造护栏：无正文时只列标题+日期+链接，绝不交给 LLM 分析
            lines.append("> ⚠️ 未取得公告正文，不做解读。")
        else:
            lines.append("### 深度分析")
            lines.append("")
            analysis = analyze_announcement(symbol, title, date, content, stock_name)
            lines.append(analysis)

            if not has_llm_api_key():
                lines.append("")
                lines.append("> ⚠️ 未配置 LLM API 密钥，以上为降级输出。")

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
