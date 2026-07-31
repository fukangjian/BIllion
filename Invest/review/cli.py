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

from config import (
    ACCOUNT_EQUITY,
    ATR_STOP_MULT,
    DRAWDOWN_STATE,
    MARKET_SCAN_OUTPUT_DIR,
    STATS_OUTPUT_DIR,
    STRATEGY_CODES,
    SYSTEM_DEFAULT_ACCOUNT,
)
from position_calculator import calc_position, format_calc_text, lookup_cluster
from review.buy_card import calc_dict_from_trade, generate_buy_card
from review.compliance_check import run_compliance_check, report_to_markdown
from review.entry_gate import check_entry, derive_state_safe, force_note, format_violations, split_by_severity
from review.metrics import compute_stats, enrich_trade_metrics, group_by_account, group_by_strategy, stats_to_markdown
from review.report_generator import generate_monthly_report, generate_weekly_report, signal_verification_section
from review.trade_log import Trade, TradeLog
from shared.utils import df_to_markdown_table, obsidian_frontmatter, write_markdown

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _resolve_cluster(symbol: str, cluster_arg: str) -> str:
    """风险簇归属：--cluster 优先，其次 config.INDUSTRY_MAP，查不到提示人工指定并记为「未指定」"""
    cluster = cluster_arg or lookup_cluster(symbol)
    if not cluster:
        print(f"[提示] {symbol} 不在 config.INDUSTRY_MAP 中，风险簇记为「未指定」，建议用 --cluster 人工指定")
        cluster = "未指定"
    return cluster


def _apply_entry_gate(trade: Trade, log: TradeLog, equity: float, force: bool,
                      drawdown_state: str | None = None) -> bool:
    """
    入场合规闸门：打印中/低级警告（继续），高级违规拒绝写入（返回 False）；
    --force 时强制放行并在备注留痕。返回是否允许写入。
    """
    violations, state = check_entry(trade, trade_log=log, account_equity=equity,
                                    drawdown_state=drawdown_state)
    print(f"[合规] 回撤状态: {state}，共 {len(violations)} 项违规")
    high, others = split_by_severity(violations)
    if others:
        print(f"[警告] {len(others)} 项中/低级违规（继续建仓）:")
        print(format_violations(others))
    if not high:
        return True
    print(f"[违规] {len(high)} 项高级违规:")
    print(format_violations(high))
    if not force:
        print("[拒绝] 存在高级违规，未写入 trades.json；确认风险后可加 --force 强制建仓")
        return False
    trade.备注 = (trade.备注 + " " if trade.备注 else "") + force_note(high)
    print("[警告] --force 生效：强制写入，备注已留痕")
    return True


def cmd_add(args):
    log = TradeLog()
    equity = args.equity or ACCOUNT_EQUITY
    trade = Trade(
        日期=args.date or datetime.now().strftime("%Y-%m-%d"),
        股票代码=args.symbol,
        股票名称=args.name or "",
        账户类型=args.account,
        风险簇=_resolve_cluster(args.symbol, args.cluster),
        入场系统=args.system,
        核心逻辑=args.logic or "",
        入场价=args.entry,
        止损价=args.stop,
        风险率=args.risk,
        股数=args.shares,
        仓位金额=args.position or (args.entry * args.shares),
        是否系统内交易=not args.off_system,
    )
    if not _apply_entry_gate(trade, log, equity, force=args.force):
        return
    log.add(trade)
    print(f"[OK] 已添加交易: {trade.交易编号}")
    print(f"     {trade.股票代码} {trade.股票名称} @ {trade.入场价}")
    card = generate_buy_card(trade, calc_dict_from_trade(trade, equity))
    if card:
        print(f"[OK] 买入卡: {card}")


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

    _write_stats_snapshot(stats, trades, args)


def _write_stats_snapshot(stats, trades: list[Trade], args) -> None:
    """统计快照写入 vault 统计目录（终端同款统计 + 信号验证统计）；写文件失败仅告警，不影响终端输出"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [
            obsidian_frontmatter(["统计", "交易复盘"], date=today),
            f"# 交易统计 · {today}",
            "",
            "## 累计统计",
            "",
            stats_to_markdown(stats),
        ]
        if getattr(args, "by_strategy", False):
            lines.extend(["", "## 按策略分组", "", df_to_markdown_table(group_by_strategy(trades))])
        if getattr(args, "by_account", False):
            lines.extend(["", "## 按账户分组", "", df_to_markdown_table(group_by_account(trades))])
        lines.extend(["", "## 信号验证（近 90 天）", "", signal_verification_section(), ""])

        path = write_markdown("\n".join(lines), STATS_OUTPUT_DIR / f"{today}_交易统计.md")
        print(f"\n[OK] 统计快照已写入: {path}")
    except Exception as e:
        print(f"\n[警告] 统计快照写入失败（不影响终端输出）: {e}")


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


def cmd_positions(args):
    """打印持仓摘要（未实现盈亏、总风险敞口、回撤状态）"""
    from review.positions import print_portfolio_summary

    log = TradeLog()
    print_portfolio_summary(log, account_equity=args.equity or ACCOUNT_EQUITY)


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


def _default_account_for_system(entry_system: str) -> str:
    """入场系统 → 默认账户类型（config.SYSTEM_DEFAULT_ACCOUNT：S1→产业、S2→核心）"""
    s = (entry_system or "").upper()
    for key, account in SYSTEM_DEFAULT_ACCOUNT.items():
        if key in s:
            return account
    return "产业"


def _execute_from_scan(args, scan_data: dict, symbol: str):
    """from-scan --execute：扫描信号 → 仓位计算 → 合规闸门 → 落库 → 买入卡"""
    symbol = symbol.zfill(6)[-6:]
    item_s1 = item_s2 = None
    for item in scan_data.get("breakout_s1a", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            item_s1 = item
    for item in scan_data.get("breakout_s2a", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            item_s2 = item

    if not item_s1 and not item_s2:
        print(f"[ERROR] {symbol} 不在今日突破候选列表中")
        return

    # 入场系统：--system 指定优先；同时出现 S1/S2 信号时默认 S2（慢速，更稳）
    if args.system:
        entry_system = args.system
        item = item_s1 if entry_system == "S1-A" else item_s2
        if not item:
            print(f"[ERROR] {symbol} 不在 {entry_system} 候选列表中")
            return
    elif item_s1 and item_s2:
        entry_system, item = "S2-A", item_s2
        print("[说明] 该股同时出现 S1-A/S2-A 信号，默认按 S2-A（慢速，更稳）建仓，可用 --system S1-A 覆盖")
    elif item_s2:
        entry_system, item = "S2-A", item_s2
    else:
        entry_system, item = "S1-A", item_s1

    close = item["close"]
    atr = item.get("atr_20", 0) or 0
    stop = round(close - atr * ATR_STOP_MULT, 2) if atr > 0 else round(close * 0.95, 2)
    equity = args.equity or ACCOUNT_EQUITY
    account = args.account or _default_account_for_system(entry_system)

    print(f"[OK] 信号: {entry_system} | 收盘价 {close:.2f} | ATR(20) {atr:.2f} | 建议止损 {stop:.2f} | 账户 {account}")

    log = TradeLog()
    state = derive_state_safe(log)

    calc = calc_position(
        equity=equity,
        entry=close,
        stop=stop,
        account_type=account,
        drawdown_state=state,
        atr=atr or None,
    )
    print(format_calc_text(calc, symbol=symbol))
    if calc["股数"] <= 0:
        print("[拒绝] 仓位计算结果为 0 股，未建仓")
        return

    logic = (
        f"{entry_system} 突破：收盘 {close:.2f} 突破 {item.get('period', '?')} 日通道高点 "
        f"{item.get('channel_high', 0):.2f}（+{item.get('breakout_pct', 0):.2f}%）"
    )
    trade = Trade(
        股票代码=symbol,
        股票名称=args.name or "",
        账户类型=account,
        风险簇=_resolve_cluster(symbol, args.cluster),
        入场系统=entry_system,
        核心逻辑=logic,
        入场价=close,
        止损价=stop,
        风险率=calc["风险率"],
        股数=calc["股数"],
        仓位金额=calc["仓位金额"],
        是否系统内交易=True,
    )
    if not _apply_entry_gate(trade, log, equity, force=args.force, drawdown_state=state):
        return
    log.add(trade)
    print(f"[OK] 已添加交易: {trade.交易编号}")
    print(f"     {trade.股票代码} {trade.股票名称} @ {trade.入场价} 止损 {trade.止损价} {trade.股数}股")
    card = generate_buy_card(trade, calc, scan_info=item)
    if card:
        print(f"[OK] 买入卡: {card}")


def cmd_from_scan(args):
    """从当天扫描 JSON 读取突破数据，输出建议入场参数（--execute 时直接落库）"""
    scan_data = _load_scan_json(args.date)
    if not scan_data:
        scan_date = args.date or datetime.now().strftime("%Y-%m-%d")
        print(f"[ERROR] 未找到扫描 JSON: market_scan_{scan_date}.json")
        print("       请先运行: python pipeline/market_scanner.py")
        return

    symbol = args.symbol.zfill(6)[-6:]

    if args.execute:
        _execute_from_scan(args, scan_data, symbol)
        return

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
    suggested_stop = round(close - atr * ATR_STOP_MULT, 2) if atr > 0 else round(close * 0.95, 2)

    print(f"[OK] 从扫描 JSON 读取 {symbol} 突破数据")
    print(f"     扫描日期:   {scan_data.get('date')}")
    print(f"     市场状态:   {scan_data.get('market_state')}")
    print(f"     入场系统:   {entry_system}")
    print(f"     收盘价:     {close:.2f}")
    print(f"     通道高点:   {channel_high:.2f}")
    print(f"     突破幅度:   {breakout.get('breakout_pct', 0):.2f}%")
    print(f"     ATR(20):    {atr:.2f}")
    print(f"     建议止损:   {suggested_stop:.2f}  (收盘价 - ATR×{ATR_STOP_MULT:g})")
    print()
    print("添加交易示例:")
    print(
        f"  python review/cli.py add {symbol} "
        f"--account 产业 --system {entry_system} "
        f"--entry {close} --stop {suggested_stop} "
        f"--risk 0.5 --shares 100"
    )
    print("或一键落库（含合规闸门与买入卡）:")
    print(f"  python review/cli.py from-scan {symbol} --execute")


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

    p_add = sub.add_parser("add", help="添加新交易（写入前自动过合规闸门）")
    p_add.add_argument("symbol", help="股票代码")
    p_add.add_argument("--name", default="", help="股票名称")
    p_add.add_argument("--date", default=None, help="入场日期 YYYY-MM-DD")
    p_add.add_argument("--account", required=True, help="账户类型: 核心/产业/事件/实验")
    p_add.add_argument("--cluster", default="", help="风险簇: 创新药/AI算力/半导体等（默认查 config.INDUSTRY_MAP）")
    p_add.add_argument("--system", required=True, choices=STRATEGY_CODES,
                       help=f"入场系统: {'/'.join(STRATEGY_CODES)}")
    p_add.add_argument("--logic", default="", help="核心逻辑")
    p_add.add_argument("--entry", type=float, required=True, help="入场价")
    p_add.add_argument("--stop", type=float, required=True, help="止损价")
    p_add.add_argument("--risk", type=float, required=True, help="风险率 (%%)")
    p_add.add_argument("--shares", type=int, required=True, help="股数")
    p_add.add_argument("--position", type=float, default=None, help="仓位金额")
    p_add.add_argument("--equity", type=float, default=None, help="账户权益（默认 config.ACCOUNT_EQUITY）")
    p_add.add_argument("--off-system", action="store_true", help="标记为非系统内交易")
    p_add.add_argument("--force", action="store_true", help="高级违规也强制写入（备注留痕）")
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

    p_pos = sub.add_parser("positions", help="持仓摘要（未实现盈亏/风险敞口/回撤状态）")
    p_pos.add_argument("--equity", type=float, default=None, help="账户权益")
    p_pos.set_defaults(func=cmd_positions)

    p_scan = sub.add_parser("from-scan", help="从扫描 JSON 读取突破数据（--execute 一键建仓落库）")
    p_scan.add_argument("symbol", help="股票代码")
    p_scan.add_argument("--date", default=None, help="扫描日期 YYYY-MM-DD（默认今天）")
    p_scan.add_argument("--execute", action="store_true", help="按信号建仓落库（含合规闸门与买入卡）")
    p_scan.add_argument("--system", choices=["S1-A", "S2-A"], default=None,
                        help="入场系统（默认：仅单一信号用该信号；同股双信号按 S2-A）")
    p_scan.add_argument("--account", default=None, help="账户类型（默认按系统映射：S1→产业、S2→核心）")
    p_scan.add_argument("--cluster", default="", help="风险簇（默认查 config.INDUSTRY_MAP）")
    p_scan.add_argument("--name", default="", help="股票名称（买入卡用）")
    p_scan.add_argument("--equity", type=float, default=None, help="账户权益（默认 config.ACCOUNT_EQUITY）")
    p_scan.add_argument("--force", action="store_true", help="高级违规也强制写入（备注留痕）")
    p_scan.set_defaults(func=cmd_from_scan)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
