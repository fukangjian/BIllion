"""
统一入口 — 盘前一键运行：数据管道 + 热点池构建 + 市场扫描（含持仓监控）+ 研究日报

用法:
    python run_all.py                    # 完整流程
    python run_all.py --skip-fetch       # 跳过数据获取，仅扫描 + 日报
    python run_all.py --pipeline-only    # 仅运行数据管道
    python run_all.py --research-only    # 仅生成研究日报
"""
import argparse
import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import DB_PATH, WATCHLIST
from pipeline.database import init_database
from pipeline.market_scanner import run_scan
from research.daily_report import generate_report
from shared.data_fetcher import fetch_and_save_all_parallel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_all")


def watchlist_with_positions() -> list[str]:
    """
    取数股票池：config.WATCHLIST ∪ trades.json 未平仓持仓股。
    保证持仓监控对任意持仓股都有行情可查；TradeLog 读取失败时降级为 WATCHLIST。
    """
    try:
        from review.positions import get_position_for_watchlist

        held = get_position_for_watchlist()
    except Exception as e:
        logger.warning("读取持仓股失败，降级使用 WATCHLIST: %s", e)
        return list(WATCHLIST)
    extra = [s for s in held if s not in WATCHLIST]
    if extra:
        logger.info("持仓股补抓 %d 只: %s", len(extra), ", ".join(extra))
    return list(WATCHLIST) + extra


def run_pipeline(skip_fetch: bool = False, symbols: list[str] | None = None) -> Path | None:
    """运行数据管道：获取数据 + 市场扫描（含持仓监控，失败自动降级）"""
    symbols = symbols or watchlist_with_positions()
    init_database()

    if not skip_fetch:
        logger.info("开始获取数据 (%d 只股票)...", len(symbols))
        try:
            stats = fetch_and_save_all_parallel(symbols, start_date="20230101")
            logger.info("数据获取完成: %s", stats)
        except Exception as e:
            logger.error("数据获取失败: %s", e)
            logger.warning("将尝试使用已有数据进行扫描...")

    # 热点池构建 + 日线补抓（超短候选来源；失败降级，不阻塞扫描与日报）
    if not skip_fetch:
        try:
            from pipeline.hot_pool import build_hot_pool, sync_hot_pool_daily

            pool = build_hot_pool()
            if pool.empty:
                logger.warning("热点池为空（数据源降级），扫描报告超短区块将标注")
            else:
                logger.info("热点池构建完成: %d 只", len(pool))
                rows = sync_hot_pool_daily(pool)
                logger.info("热点池日线补抓完成: %d 行", rows)
        except Exception as e:
            logger.warning("热点池构建失败（已降级，不影响扫描与日报）: %s", e)

    # 趋势池构建 + 日线补抓（强势板块成分股，趋势扫描候选来源；失败降级同上）
    if not skip_fetch:
        try:
            from pipeline.trend_pool import build_trend_pool, sync_trend_pool_daily

            tpool = build_trend_pool()
            if tpool.empty:
                logger.warning("趋势池为空（数据源降级），趋势扫描仅覆盖 WATCHLIST")
            else:
                logger.info("趋势池构建完成: %d 只", len(tpool))
                rows = sync_trend_pool_daily(tpool)
                logger.info("趋势池日线补抓完成: %d 行", rows)
        except Exception as e:
            logger.warning("趋势池构建失败（已降级，不影响扫描与日报）: %s", e)

    logger.info("开始市场扫描（含持仓监控）...")
    report_path = None
    try:
        report_path = run_scan(symbols=symbols)
        logger.info("市场扫描完成: %s", report_path)
    except Exception as e:
        # 扫描/监控失败不阻塞研究日报
        logger.error("市场扫描失败（降级继续研究日报）: %s", e)

    # 信号每日结算：扫描（含新信号入库）之后执行；
    # settle 内部以 signal_date < 当天 过滤，当天新记录的信号不会被立即结算
    try:
        from pipeline.signal_tracker import settle_signals

        sr = settle_signals()
        logger.info(
            "信号结算完成: 检查 %d 个 open 信号，关闭 %d 个（%s）",
            sr["checked"], sr["settled"], sr["by_reason"],
        )
    except Exception as e:
        logger.warning("信号结算失败（已降级，不影响盘前流程）: %s", e)

    return report_path


def run_research(watchlist: list[str] | None = None, sectors: list[str] | None = None) -> Path:
    """生成每日研究日报"""
    logger.info("开始生成研究日报...")
    report_path = generate_report(watchlist=watchlist, sectors=sectors)
    logger.info("研究日报完成: %s", report_path)
    return report_path


def main():
    parser = argparse.ArgumentParser(description="Invest 统一入口 — 数据管道 + 研究日报")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="管道扫描股票列表（默认 config.WATCHLIST）",
    )
    parser.add_argument(
        "-w", "--watchlist",
        nargs="*",
        default=None,
        help="研究日报关注股票（默认 config.DEFAULT_WATCHLIST）",
    )
    parser.add_argument(
        "-s", "--sectors",
        nargs="*",
        default=None,
        help="研究日报关注板块",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="跳过数据获取，使用已有数据库",
    )
    parser.add_argument(
        "--pipeline-only",
        action="store_true",
        help="仅运行数据管道（获取 + 扫描）",
    )
    parser.add_argument(
        "--research-only",
        action="store_true",
        help="仅生成研究日报",
    )
    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info("Invest 统一工作流启动 — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("数据库: %s", DB_PATH)
    logger.info("=" * 50)

    scan_path = None
    daily_path = None

    try:
        if not args.research_only:
            scan_path = run_pipeline(skip_fetch=args.skip_fetch, symbols=args.symbols)

        if not args.pipeline_only:
            daily_path = run_research(watchlist=args.watchlist, sectors=args.sectors)

        print("\n" + "=" * 50)
        print("完成!")
        if scan_path:
            print(f"  市场扫描: {scan_path}")
        if daily_path:
            print(f"  研究日报: {daily_path}")
        print("=" * 50)

    except Exception as e:
        logger.error("工作流失败: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
