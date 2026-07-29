"""
交易复盘 CLI — 命令行界面
"""
import argparse
import json
import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import ACCOUNT_EQUITY, DRAWDOWN_STATE, MARKET_SCAN_OUTPUT_DIR
from review.compliance_check import run_compliance_check, report_to_markdown
from review.metrics import compute_stats, enrich_trade_metrics, group_by_account, group_by_strategy, stats_to_markdown
from review.report_generator import generate_monthly_report, generate_weekly_report
from review.trade_log import Trade, TradeLog

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def cmd_add(args):
    log = TradeLog()
    trade = Trade(
        日期=args.date or datetime.now().strftime("%Y-%m-%d"),
        股票代码=args.symbol,
        股票名称=args.name or "",
        账户类型=args.account,
        风险簇=args.cluster or "",
        入场系统=args.system,
        核心逻辑=args.logic or "",
        入场价=args.entry,
        止损价=args.stop,
        风险率=args.risk,
        股数=args.shares,
        仓位金额=args.position or (args.entry * args.shares),
        是否系统内交易=not args.off_system,
    )
    log.add(trade)
    print(f"[OK] 已添加交易: {trade.交易编号}")
    print(f"     {trade.股票代码} {trade.股票名称} @ {trade.入场价}")


def cmd_update(args):
    log = TradeLog()
    updates = {}

    if args.exit_price is not None:
        updates["实际退出价"] = args.exit_price
    if args.exit_date:
        updates["退出日期"] = args.exit_date
    if args.exit_reason:
        updates["退出原因"] = args.exit_reason
    if args.shares is not None:
        updates["股数"] = args.shares
    if args.stop is not None:
        updates["止损价"] = args.stop
    if args.mfe is not None:
        updates["MFE"] = args.mfe
    if args.mae is not None:
        updates["MAE"] = args.mae
    if args.note:
        updates["备注"] = args.note

    if not updates:
        print("[ERROR] 未指定更新字段")
        return

    trade = log.update(args.id, **updates)
    if not trade:
        print(f"[ERROR] 未找到交易: {args.id}")
        return

    if trade.is_closed:
        from review.metrics import calc_r_multiple
        r = calc_r_multiple(trade.入场价, trade.实际退出价, trade.止损价)
        if r is not None:
            log.update(args.id, R倍数=round(r, 2))
            trade.R倍数 = round(r, 2)

        if args.fetch_prices:
            enrich_trade_metrics(trade, fetch_prices=True)
            log.update(args.id, MFE=trade.MFE, MAE=trade.MAE)

    print(f"[OK] 已更新交易: {args.id}")
    if trade.R倍数 is not None:
        print(f"   R 倍数: {trade.R倍数}")


def cmd_list(args):
    log = TradeLog()
    if args.closed:
        trades = log.list_all(closed_only=True)
    elif args.open:
        trades = log.list_all(open_only=True)
    else:
        trades = log.list_all()

    if not trades:
        print("暂无交易记录")
        return

    print(f"{'编号':<16} {'日期':<12} {'代码':<8} {'名称':<8} {'系统':<8} {'R':<6} {'状态'}")
    print("-" * 70)
    for t in trades:
        r = f"{t.R倍数:.1f}" if t.R倍数 is not None else "-"
        status = "已平" if t.is_closed else "持仓"
        print(f"{t.交易编号:<16} {t.日期:<12} {t.股票代码:<8} {t.股票名称:<8} {t.入场系统:<8} {r:<6} {status}")


def cmd_stats(args):
    log = TradeLog()
    trades = log.list_all(closed_only=args.closed)

    if args.fetch_prices:
        for t in trades:
            if t.is_closed:
                enrich_trade_metrics(t, fetch_prices=True)

    stats = compute_stats(trades, fetch_prices=False)
    print("\n=== 累计统计 ===\n")
    print(stats_to_markdown(stats))

    if args.by_strategy:
        print("\n=== 按策略分组 ===\n")
        df = group_by_strategy(trades)
        print(df.to_string(index=False) if not df.empty else "无数据")

    if args.by_account:
        print("\n=== 按账户分组 ===\n")
        df = group_by_account(trades)
        print(df.to_string(index=False) if not df.empty else "无数据")


def cmd_weekly(args):
    path = generate_weekly_report(fetch_prices=args.fetch_prices)
    print(f"[OK] 周报已生成: {path}")


def cmd_monthly(args):
    path = generate_monthly_report(fetch_prices=args.fetch_prices)
    print(f"[OK] 月报已生成: {path}")


def cmd_check(args):
    log = TradeLog()
    trades = log.list_all()
    report = run_compliance_check(
        trades,
        drawdown_state=args.drawdown or DRAWDOWN_STATE,
        account_equity=args.equity or ACCOUNT_EQUITY,
    )
    print(report_to_markdown(report))


def _load_scan_json(date: str | None = None) -> dict | None:
    """加载指定日期的市场扫描 JSON"""
    scan_date = date or datetime.now().strftime("%Y-%m-%d")
    json_path = MARKET_SCAN_OUTPUT_DIR / f"market_scan_{scan_date}.json"
    if not json_path.exists():
        return None
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_breakout_in_scan(scan_data: dict, symbol: str) -> tuple[dict | None, str | None]:
    """在扫描 JSON 中查找股票突破数据，返回 (数据, 入场系统)"""
    symbol = symbol.zfill(6)[-6:]
    for item in scan_data.get("breakout_s1a", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            return item, "S1-A"
    for item in scan_data.get("breakout_s2a", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            return item, "S2-A"
    return None, None


def cmd_from_scan(args):
    """从当天扫描 JSON 读取突破数据，输出建议入场参数"""
    scan_data = _load_scan_json(args.date)
    if not scan_data:
        scan_date = args.date or datetime.now().strftime("%Y-%m-%d")
        print(f"[ERROR] 未找到扫描 JSON: market_scan_{scan_date}.json")
        print("       请先运行: python pipeline/market_scanner.py")
        return

    symbol = args.symbol.zfill(6)[-6:]
    breakout, entry_system = _find_breakout_in_scan(scan_data, symbol)
    if not breakout:
        print(f"[ERROR] {symbol} 不在今日突破候选列表中")
        print(f"       扫描日期: {scan_data.get('date')}")
        print(f"       市场状态: {scan_data.get('market_state')}")
        s1 = [x["symbol"] for x in scan_data.get("breakout_s1a", [])]
        s2 = [x["symbol"] for x in scan_data.get("breakout_s2a", [])]
        if s1:
            print(f"       S1-A 候选: {', '.join(s1)}")
        if s2:
            print(f"       S2-A 候选: {', '.join(s2)}")
        return

    close = breakout["close"]
    channel_high = breakout["channel_high"]
    atr = breakout.get("atr_20", 0) or 0
    suggested_stop = round(close - atr * 2, 2) if atr > 0 else round(close * 0.95, 2)

    print(f"[OK] 从扫描 JSON 读取 {symbol} 突破数据")
    print(f"     扫描日期:   {scan_data.get('date')}")
    print(f"     市场状态:   {scan_data.get('market_state')}")
    print(f"     入场系统:   {entry_system}")
    print(f"     收盘价:     {close:.2f}")
    print(f"     通道高点:   {channel_high:.2f}")
    print(f"     突破幅度:   {breakout.get('breakout_pct', 0):.2f}%")
    print(f"     ATR(20):    {atr:.2f}")
    print(f"     建议止损:   {suggested_stop:.2f}  (收盘价 - ATR×2)")
    print()
    print("添加交易示例:")
    print(
        f"  python review/cli.py add {symbol} "
        f"--account 产业 --system {entry_system} "
        f"--entry {close} --stop {suggested_stop} "
        f"--risk 0.5 --shares 100"
    )


def cmd_show(args):
    log = TradeLog()
    trade = log.get(args.id)
    if not trade:
        print(f"[ERROR] 未找到: {args.id}")
        return
    print(json.dumps(trade.to_dict(), ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="交易复盘自动化工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    p_add = sub.add_parser("add", help="添加新交易")
    p_add.add_argument("symbol", help="股票代码")
    p_add.add_argument("--name", default="", help="股票名称")
    p_add.add_argument("--date", default=None, help="入场日期 YYYY-MM-DD")
    p_add.add_argument("--account", required=True, help="账户类型: 核心/产业/事件/实验")
    p_add.add_argument("--cluster", default="", help="风险簇: 创新药/AI算力/半导体等")
    p_add.add_argument("--system", required=True, help="入场系统: S1-A/S2-A/预埋")
    p_add.add_argument("--logic", default="", help="核心逻辑")
    p_add.add_argument("--entry", type=float, required=True, help="入场价")
    p_add.add_argument("--stop", type=float, required=True, help="止损价")
    p_add.add_argument("--risk", type=float, required=True, help="风险率 (%)")
    p_add.add_argument("--shares", type=int, required=True, help="股数")
    p_add.add_argument("--position", type=float, default=None, help="仓位金额")
    p_add.add_argument("--off-system", action="store_true", help="标记为非系统内交易")
    p_add.set_defaults(func=cmd_add)

    p_upd = sub.add_parser("update", help="更新交易")
    p_upd.add_argument("id", help="交易编号")
    p_upd.add_argument("--exit-price", type=float, default=None, help="退出价")
    p_upd.add_argument("--exit-date", default=None, help="退出日期")
    p_upd.add_argument("--exit-reason", default=None, help="退出原因")
    p_upd.add_argument("--shares", type=int, default=None, help="更新股数")
    p_upd.add_argument("--stop", type=float, default=None, help="更新止损价")
    p_upd.add_argument("--mfe", type=float, default=None, help="MFE (R)")
    p_upd.add_argument("--mae", type=float, default=None, help="MAE (R)")
    p_upd.add_argument("--note", default=None, help="备注")
    p_upd.add_argument("--fetch-prices", action="store_true", help="自动获取 MFE/MAE")
    p_upd.set_defaults(func=cmd_update)

    p_list = sub.add_parser("list", help="列出交易")
    p_list.add_argument("--closed", action="store_true", help="仅已平仓")
    p_list.add_argument("--open", action="store_true", help="仅持仓中")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="查看交易详情")
    p_show.add_argument("id", help="交易编号")
    p_show.set_defaults(func=cmd_show)

    p_stats = sub.add_parser("stats", help="累计统计")
    p_stats.add_argument("--closed", action="store_true", help="仅统计已平仓")
    p_stats.add_argument("--by-strategy", action="store_true", help="按策略分组")
    p_stats.add_argument("--by-account", action="store_true", help="按账户分组")
    p_stats.add_argument("--fetch-prices", action="store_true", help="自动获取 MFE/MAE")
    p_stats.set_defaults(func=cmd_stats)

    p_weekly = sub.add_parser("weekly", help="生成周报")
    p_weekly.add_argument("--fetch-prices", action="store_true", help="自动获取 MFE/MAE")
    p_weekly.set_defaults(func=cmd_weekly)

    p_monthly = sub.add_parser("monthly", help="生成月报")
    p_monthly.add_argument("--fetch-prices", action="store_true", help="自动获取 MFE/MAE")
    p_monthly.set_defaults(func=cmd_monthly)

    p_check = sub.add_parser("check", help="合规检查")
    p_check.add_argument("--drawdown", default=None, help="回撤状态: Normal/Caution/Defensive/Review")
    p_check.add_argument("--equity", type=float, default=None, help="账户权益")
    p_check.set_defaults(func=cmd_check)

    p_scan = sub.add_parser("from-scan", help="从扫描 JSON 读取突破数据")
    p_scan.add_argument("symbol", help="股票代码")
    p_scan.add_argument("--date", default=None, help="扫描日期 YYYY-MM-DD（默认今天）")
    p_scan.set_defaults(func=cmd_from_scan)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
