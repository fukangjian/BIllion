"""
每日日报运行入口 — 一键生成每日研究日报
"""
import argparse
import logging
import sys
from pathlib import Path

# 确保模块路径正确
sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_report import generate_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="每日研究日报 — 一键生成 Obsidian 兼容的研究日报",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run_daily_report.py
  python run_daily_report.py -w 688192 600519 -s 创新药 AI算力
  python run_daily_report.py -o "e:\\doc\\阅读书籍\\【9】AI\\daily_reports"
        """,
    )
    parser.add_argument(
        "-w", "--watchlist",
        nargs="*",
        default=None,
        help="关注股票代码（默认使用 config.DEFAULT_WATCHLIST）",
    )
    parser.add_argument(
        "-s", "--sectors",
        nargs="*",
        default=None,
        help="关注板块关键词（默认: 创新药 AI算力 半导体 新能源）",
    )
    parser.add_argument("-o", "--output", type=str, default=None, help="输出目录")
    args = parser.parse_args()

    output_dir = Path(args.output) if args.output else None

    logger.info("开始生成每日研究日报...")
    try:
        path = generate_report(
            watchlist=args.watchlist,
            sectors=args.sectors,
            output_dir=output_dir,
        )
        print(f"\n[OK] 日报已生成: {path}")
        print(f"   可在 Obsidian 中打开查看")
    except Exception as e:
        logger.error("日报生成失败: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
