"""
每日运行入口 — 获取数据 + 市场扫描 + 生成报告
"""
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# 确保模块路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import WATCHLIST, OUTPUT_DIR, DB_PATH
from database import init_database
from data_fetcher import fetch_and_save_all
from market_scanner import run_scan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_daily")


def main():
    parser = argparse.ArgumentParser(description="每日量化数据管道 — 获取数据并生成扫描报告")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=WATCHLIST,
        help="股票代码列表，默认使用 config.WATCHLIST",
    )
    parser.add_argument(
        "--start-date",
        default="20230101",
        help="历史数据起始日期 (YYYYMMDD)",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="跳过数据获取，仅运行扫描（使用已有数据库）",
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="仅获取数据，不运行扫描",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(OUTPUT_DIR),
        help="报告输出目录",
    )
    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info("量化数据管道启动 — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("数据库: %s", DB_PATH)
    logger.info("=" * 50)

    # 初始化数据库
    init_database()

    # 数据获取
    if not args.skip_fetch:
        logger.info("开始获取数据 (%d 只股票)...", len(args.symbols))
        try:
            stats = fetch_and_save_all(args.symbols, start_date=args.start_date)
            logger.info("数据获取完成: %s", stats)
        except Exception as e:
            logger.error("数据获取失败: %s", e)
            if not args.fetch_only:
                logger.warning("将尝试使用已有数据进行扫描...")

    if args.fetch_only:
        logger.info("仅获取模式，退出")
        return

    # 市场扫描
    logger.info("开始市场扫描...")
    try:
        report_path = run_scan(
            symbols=args.symbols,
            output_dir=Path(args.output_dir),
        )
        logger.info("扫描完成，报告: %s", report_path)
        print(f"\n报告已生成: {report_path}")
    except Exception as e:
        logger.error("扫描失败: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
