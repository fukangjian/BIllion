"""
交易复盘 CLI — 命令行界面
"""
import argparse
import json
import logging
import sys
import warnings
from datetime import date, datetime
from pathlib import Path

warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    ACCOUNT_EQUITY,
    ATR_STOP_MULT,
    DRAWDOWN_STATE,
    MARKET_SCAN_OUTPUT_DIR,
    SIGNAL_MAX_HOLDING_BY_SYSTEM,
    STATS_OUTPUT_DIR,
    STRATEGY_CODES,
    SYSTEM_DEFAULT_ACCOUNT,
)
from pipeline.database import load_daily_quotes, load_hot_pool
from position_calculator import calc_position, format_calc_text, lookup_cluster
from review.buy_card import calc_dict_from_trade, generate_buy_card
from review.compliance_check import run_compliance_check, report_to_markdown
from review.discipline_audit import audit_discipline, audit_to_markdown
from review.entry_gate import check_entry, derive_state_safe, force_note, format_violations, split_by_severity
from review.metrics import compute_stats, enrich_trade_metrics, group_by_account, group_by_strategy, stats_to_markdown
from review.monitor import _check_single_position
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
        入场时间=getattr(args, "time", "") or "",
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
    if getattr(args, "exit_time", None):
        updates["退出时间"] = args.exit_time
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

    # 纪律审计摘要（全部 trades）；审计失败仅警告，不影响合规检查输出
    try:
        print("\n=== 纪律审计 ===\n")
        print(audit_to_markdown(audit_discipline(trades)))
    except Exception as e:
        print(f"[警告] 纪律审计不可用: {e}")


def cmd_import(args):
    """券商成交导入：解析 → FIFO 配对 → 落库（历史事实，不过入场闸门）→ 导入后自动合规检查"""
    from review.import_broker import (
        import_records,
        load_records_from_file,
        parse_symbol_map,
        print_import_summary,
    )

    path = Path(args.file)
    if not path.exists():
        print(f"[ERROR] 文件不存在: {path}")
        return
    try:
        records = load_records_from_file(path, args.year)
    except ValueError as e:
        print(f"[ERROR] {e}")
        return
    if not records:
        print("[ERROR] 未解析到任何成交记录，请检查文件格式（参考 review/import_broker.py 模块 docstring）")
        return
    print(f"[OK] 解析成交记录 {len(records)} 条（年份 {args.year}）")

    log = TradeLog()
    result = import_records(
        records,
        log,
        symbol_map=parse_symbol_map(args.symbol_map),
        account_type=args.account,
        entry_system=args.system,
        dry_run=args.dry_run,
    )
    print_import_summary(result, dry_run=args.dry_run)
    if args.dry_run:
        return

    # 导入后自动跑一遍合规检查（如实报告「缺少止损」等历史问题——这正是复盘价值）
    print("\n=== 导入后合规检查 ===\n")
    report = run_compliance_check(log.list_all())
    print(report_to_markdown(report))


def cmd_positions(args):
    """打印持仓摘要（未实现盈亏、总风险敞口、回撤状态）"""
    from review.positions import print_portfolio_summary

    log = TradeLog()
    print_portfolio_summary(log, account_equity=args.equity or ACCOUNT_EQUITY)


# ---------- sell-check 卖出助手 ----------

def _fmt_opt(v: float | None, signed: bool = False) -> str:
    """可选数值格式化（卖点检查单用，None → "-"）"""
    if v is None:
        return "-"
    return f"{v:+.2f}" if signed else f"{v:.2f}"


def build_sell_check_lines(
    trade: Trade,
    position_info: dict | None,
    in_hot_pool: bool | None,
    close: float | None,
    today: date | None = None,
) -> list[str]:
    """
    组装卖点检查单文本（纯函数，便于单测）。

    - position_info: monitor._check_single_position 的返回（None 表示检查执行失败）；
    - in_hot_pool: None 表示热点池为空或读取失败（降级），True/False 为是否在池；
    - close: 最新收盘价（None 表示无行情，建议挂单价不可用）；
    - today: 持有天数基准日（默认今天，测试可注入）。
    """
    today = today or datetime.now().date()
    lines = [
        f"===== 卖点检查 {trade.股票代码} {trade.股票名称} =====",
        f"交易编号: {trade.交易编号} | 账户: {trade.账户类型} | 入场系统: {trade.入场系统 or '未记录'}",
        f"入场: {trade.日期} @ {trade.入场价:.2f} | 止损价: {trade.止损价:.2f}",
    ]

    # 1) 止损 / 退出通道状态（monitor._check_single_position 口径）
    if position_info is None:
        lines.append("行情检查: 不可用（持仓监控执行失败，已降级）")
    elif position_info.get("现价") is None:
        note = position_info.get("备注") or "market.db 无该股日线"
        lines.append(f"行情检查: 无行情数据（{note}，止损/通道状态不可用）")
    else:
        lines.append(
            f"最新收盘: {position_info['现价']:.2f}（数据日期 {position_info.get('数据日期')}）"
            f" | 浮动R: {_fmt_opt(position_info.get('浮动R'), signed=True)}"
            f" | 通道下轨: {_fmt_opt(position_info.get('通道下轨'))}"
            f" | 距止损: {_fmt_opt(position_info.get('距止损N'))}N"
        )
        if position_info.get("类型"):
            lines.append(f"⚠️ 警报 [{position_info['类型']}]: {position_info.get('建议动作')}")
        else:
            lines.append("警报: 无（未触发止损/退出通道）")

    # 2) 持有天数（自然日）与 HOT-S 强制离场提示（交易日上限，自然日近似）
    try:
        entry_date = datetime.strptime(trade.日期, "%Y-%m-%d").date()
        holding_days = (today - entry_date).days
    except (ValueError, TypeError):
        holding_days = None
    if holding_days is None:
        lines.append(f"持有天数: 不可用（入场日期无法解析: {trade.日期!r}）")
    else:
        lines.append(f"持有天数: {holding_days} 天（自然日，自 {trade.日期} 起）")
        max_hold = SIGNAL_MAX_HOLDING_BY_SYSTEM.get(trade.入场系统)
        if max_hold is not None:
            remain = max_hold - holding_days
            basis = f"自然日近似，口径 config.SIGNAL_MAX_HOLDING_BY_SYSTEM（{trade.入场系统} {max_hold} 个交易日强制离场）"
            if remain > 0:
                lines.append(f"强制离场: 约剩 {remain} 天（{basis}）")
            else:
                lines.append(f"⚠️ 强制离场: 已到期/超期，建议今日卖出（{basis}）")

    # 3) 热点池标注
    if in_hot_pool is None:
        lines.append("热点池: 不可用（热点池为空或读取失败，已降级）")
    elif in_hot_pool:
        lines.append("热点池: 仍在热点池")
    else:
        lines.append("热点池: 已不在热点池（超短逻辑可能已失效，关注离场）")

    # 4) 建议挂单价 = 最新收盘 × 0.99（日志教训：挂低 1% 防挂高未成交）
    if close is not None and close > 0:
        lines.append(
            f"建议挂单价: {round(close * 0.99, 2):.2f}"
            f"（= 最新收盘 {close:.2f} × 0.99；日志教训：挂低 1% 防挂高未成交）"
        )
    else:
        lines.append("建议挂单价: 不可用（无行情数据）")

    return lines


def _symbol_in_hot_pool(symbol: str) -> bool | None:
    """查询代码是否在最新一期热点池（离线 hot_pool 表）；池为空/读取失败返回 None（降级）"""
    try:
        pool = load_hot_pool()
        if pool.empty:
            return None
        latest_date = pool["trade_date"].max()
        latest = pool[pool["trade_date"] == latest_date]
        symbols = {str(s).zfill(6)[-6:] for s in latest["symbol"]}
        return symbol in symbols
    except Exception as e:
        logger.warning("热点池读取失败（sell-check 降级）: %s", e)
        return None


def cmd_sell_check(args):
    """卖点检查单：止损/退出通道警报 + 持有天数 + 热点池 + 建议挂单价（全程离线，异常降级不崩）"""
    log = TradeLog()
    symbol = args.symbol.zfill(6)[-6:]

    candidates = [t for t in log.get_by_symbol(symbol) if not t.is_closed]
    if not candidates:
        print(f"[提示] {symbol} 当前无持仓中交易，无需卖点检查")
        open_trades = log.list_all(open_only=True)
        if open_trades:
            holdings = ", ".join(f"{t.股票代码} {t.股票名称}".strip() for t in open_trades)
            print(f"       当前持仓代码: {holdings}")
        else:
            print("       当前无任何持仓")
        return

    trade = max(candidates, key=lambda t: (t.日期 or "", t.创建时间 or ""))  # 多笔持仓取最新一笔

    position_info = None
    try:
        position_info = _check_single_position(trade)
    except Exception as e:
        logger.warning("持仓检查失败（sell-check 降级）: %s", e)

    in_hot_pool = _symbol_in_hot_pool(symbol)
    close = position_info.get("现价") if position_info else None

    for line in build_sell_check_lines(trade, position_info, in_hot_pool, close):
        print(line)


def cmd_add_position(args):
    """金字塔加仓（V5.0 §5.5 三档）：触发判定 → 仓位建议 → 合规闸门 → 落库 → 全链止损上移"""
    import pandas as pd

    from pipeline.indicators import calc_atr
    from review.pyramid import add_unit_shares, check_add_trigger, get_unit_chain

    symbol = args.symbol.zfill(6)[-6:]
    log = TradeLog()
    chain = get_unit_chain(log.list_all(open_only=True), symbol)
    if not chain:
        print(f"[ERROR] {symbol} 无未平仓单位链（需先建仓）")
        return
    root = chain[0]

    df = load_daily_quotes(symbol=symbol)
    if df.empty:
        print(f"[ERROR] market.db 无 {symbol} 行情，无法判定加仓触发")
        return
    df = df.sort_values("trade_date").reset_index(drop=True)
    latest_close = float(df.iloc[-1]["close"])
    atr_series = calc_atr(df)
    atr = float(atr_series.iloc[-1]) if not atr_series.empty and pd.notna(atr_series.iloc[-1]) else 0.0

    price = args.price or latest_close  # 实际成交价可覆盖（默认最新收盘）
    trig = check_add_trigger(chain, price, atr)
    print(f"[加仓判定] 现价 {price:.2f} | ATR {atr:.2f} | 当前 {len(chain)} 单位 | {trig['原因']}")
    if not trig["可加仓"]:
        return

    state = derive_state_safe(log)
    if state != "Normal":
        print(f"[拒绝] 回撤状态 {state}（非 Normal）禁止加仓（V5.0 §5.5：账户回撤降档禁止加仓）")
        return

    new_stop = trig["统一止损价"]
    per_share_risk = price - new_stop
    shares = add_unit_shares(root, per_share_risk)
    if shares <= 0:
        print(f"[拒绝] 建议股数不足一手（每股风险 {per_share_risk:.2f}），未加仓")
        return

    equity = args.equity or ACCOUNT_EQUITY
    trade = Trade(
        股票代码=symbol,
        股票名称=args.name or root.股票名称,
        账户类型=root.账户类型,
        风险簇=root.风险簇,
        入场系统=root.入场系统,
        核心逻辑=f"金字塔加仓（首仓 {root.交易编号}，触发价 {trig['触发价']:.2f}）",
        入场价=price,
        止损价=new_stop,
        风险率=round(per_share_risk * shares / equity * 100, 3),
        股数=shares,
        仓位金额=round(price * shares, 2),
        是否系统内交易=True,
        关联单号=root.交易编号,
        单位序号=trig["下一单位序号"],
        备注=f"加仓 N={atr:.2f}，统一止损 {new_stop:.2f}",
    )
    if not _apply_entry_gate(trade, log, equity, force=args.force, drawdown_state=state):
        return
    log.add(trade)
    print(f"[OK] 加仓落库: {trade.交易编号}（单位{trade.单位序号}）@ {price:.2f} {shares}股，止损 {new_stop:.2f}")

    # 海龟统一止损：全链未平仓单位止损上移（只上不下，旧止损写入备注留痕）
    for t in chain:
        if new_stop > t.止损价:
            note = (t.备注 + f" [加仓后止损上移 {t.止损价:.2f}→{new_stop:.2f}]").strip()
            log.update(t.交易编号, 止损价=new_stop, 备注=note)
            print(f"[OK] {t.交易编号} 止损上移 → {new_stop:.2f}")


def cmd_sell(args):
    """卖出登记：全平直接闭环（自动算 R）；部分卖出拆单（原单减股数 + 新增已平仓子单）"""
    from review.metrics import calc_r_multiple

    symbol = args.symbol.zfill(6)[-6:]
    log = TradeLog()

    if args.id:
        target = log.get(args.id)
        if not target or target.is_closed:
            print(f"[ERROR] {args.id} 不存在或已平仓")
            return
    else:
        candidates = [t for t in log.get_by_symbol(symbol) if not t.is_closed]
        if not candidates:
            print(f"[提示] {symbol} 当前无持仓中交易")
            return
        target = max(candidates, key=lambda t: (t.日期 or "", t.创建时间 or ""))

    exit_date = args.date or datetime.now().strftime("%Y-%m-%d")
    reason = args.reason or ""
    total = target.股数

    if args.shares >= total:
        # 全平：与 update 子命令同口径（退出价/日期 + 自动算 R）
        log.update(target.交易编号, 实际退出价=args.price, 退出日期=exit_date,
                   退出时间=args.time or "", 退出原因=reason)
        r = calc_r_multiple(target.入场价, args.price, target.止损价)
        if r is not None:
            log.update(target.交易编号, R倍数=round(r, 2))
            print(f"[OK] 全平: {target.交易编号} {target.股票代码} @ {args.price:.2f}（R {r:+.2f}）")
        else:
            print(f"[OK] 全平: {target.交易编号} {target.股票代码} @ {args.price:.2f}")
    else:
        # 部分卖出：原单减股数，新增已平仓拆分子单（每条记录 R 口径独立）
        remain = total - args.shares
        note = (target.备注 + f" [分批卖出 {args.shares}股 @ {args.price:.2f}，余 {remain}股]").strip()
        log.update(target.交易编号, 股数=remain,
                   仓位金额=round(target.入场价 * remain, 2), 备注=note)
        child = Trade(
            日期=target.日期,
            入场时间=target.入场时间,
            股票代码=target.股票代码,
            股票名称=target.股票名称,
            账户类型=target.账户类型,
            风险簇=target.风险簇,
            入场系统=target.入场系统,
            核心逻辑=target.核心逻辑,
            入场价=target.入场价,
            止损价=target.止损价,
            风险率=round(target.per_share_risk * args.shares
                         / (args.equity or ACCOUNT_EQUITY) * 100, 3),
            股数=args.shares,
            仓位金额=round(target.入场价 * args.shares, 2),
            实际退出价=args.price,
            退出日期=exit_date,
            退出时间=args.time or "",
            退出原因=reason or "分批止盈",
            是否系统内交易=target.是否系统内交易,
            关联单号=target.交易编号,
            单位序号=target.单位序号,
            备注=f"分批卖出（来源 {target.交易编号}）",
        )
        r = calc_r_multiple(child.入场价, args.price, child.止损价)
        if r is not None:
            child.R倍数 = round(r, 2)
        log.add(child)
        r_text = f"（R {r:+.2f}）" if r is not None else ""
        print(f"[OK] 部分卖出: {target.交易编号} 减 {args.shares}股（余 {remain}股）"
              f"→ 子单 {child.交易编号} @ {args.price:.2f}{r_text}")

    print("[提示] 重新入场条件（V5.0 §7.6）：新的 20/55 日突破 + 板块重新共振 + "
          "失败原因已消失 + 新的止损与风险预算可计算；不因「刚卖掉」产生心理抵触")


def _load_scan_json(date: str | None = None) -> dict | None:
    """加载指定日期的市场扫描 JSON"""
    scan_date = date or datetime.now().strftime("%Y-%m-%d")
    json_path = MARKET_SCAN_OUTPUT_DIR / f"market_scan_{scan_date}.json"
    if not json_path.exists():
        return None
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_breakout_in_scan(scan_data: dict, symbol: str) -> tuple[dict | None, str | None]:
    """在扫描 JSON 中查找股票突破数据，返回 (数据, 入场系统)；热点池突破候选系统为 HOT-S"""
    symbol = symbol.zfill(6)[-6:]
    for item in scan_data.get("breakout_s1a", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            return item, "S1-A"
    for item in scan_data.get("breakout_s2a", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            return item, "S2-A"
    for item in (scan_data.get("hot_pool") or {}).get("hot_breakout", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            return item, "HOT-S"
    return None, None


def _default_account_for_system(entry_system: str) -> str:
    """入场系统 → 默认账户类型（config.SYSTEM_DEFAULT_ACCOUNT：S1→产业、S2→核心；HOT-S→事件）"""
    s = (entry_system or "").upper()
    # HOT-S 映射「事件」（config 未含该键，超短热点账户依投资体系 V5.0 在 cli 本地特判）
    if "HOT" in s:
        return "事件"
    for key, account in SYSTEM_DEFAULT_ACCOUNT.items():
        if key in s:
            return account
    return "产业"


def _execute_from_scan(args, scan_data: dict, symbol: str):
    """from-scan --execute：扫描信号 → 仓位计算 → 合规闸门 → 落库 → 买入卡"""
    symbol = symbol.zfill(6)[-6:]
    item_s1 = item_s2 = item_hot = None
    for item in scan_data.get("breakout_s1a", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            item_s1 = item
    for item in scan_data.get("breakout_s2a", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            item_s2 = item
    for item in (scan_data.get("hot_pool") or {}).get("hot_breakout", []):
        if str(item.get("symbol", "")).zfill(6)[-6:] == symbol:
            item_hot = item

    if not item_s1 and not item_s2 and not item_hot:
        print(f"[ERROR] {symbol} 不在今日突破候选列表中")
        return

    # 入场系统：--system 指定优先；同时出现 S1/S2 信号时默认 S2（慢速，更稳）；仅热点信号时按 HOT-S
    if args.system:
        entry_system = args.system
        item = {"S1-A": item_s1, "S2-A": item_s2, "HOT-S": item_hot}.get(entry_system)
        if not item:
            print(f"[ERROR] {symbol} 不在 {entry_system} 候选列表中")
            return
    elif item_s1 and item_s2:
        entry_system, item = "S2-A", item_s2
        print("[说明] 该股同时出现 S1-A/S2-A 信号，默认按 S2-A（慢速，更稳）建仓，可用 --system S1-A 覆盖")
    elif item_s2:
        entry_system, item = "S2-A", item_s2
    elif item_s1:
        entry_system, item = "S1-A", item_s1
    else:
        entry_system, item = "HOT-S", item_hot
    if item_hot and entry_system != "HOT-S":
        print("[说明] 该股另有热点池突破信号（HOT-S 超短），可用 --system HOT-S 选择")

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
    if entry_system == "HOT-S" and item.get("source"):
        logic += f"；热点来源 {item['source']} {item.get('sector', '')}".rstrip()
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
        hot = [x["symbol"] for x in (scan_data.get("hot_pool") or {}).get("hot_breakout", [])]
        if s1:
            print(f"       S1-A 候选: {', '.join(s1)}")
        if s2:
            print(f"       S2-A 候选: {', '.join(s2)}")
        if hot:
            print(f"       HOT-S 候选: {', '.join(hot)}")
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
        f"--account {_default_account_for_system(entry_system)} --system {entry_system} "
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
    p_add.add_argument("--time", default="", help="入场时间 HH:MM:SS（可选，纪律审计用）")
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
    p_upd.add_argument("--exit-time", default=None, help="退出时间 HH:MM:SS（可选，纪律审计用）")
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

    p_import = sub.add_parser("import", help="券商成交导入（Markdown 表 / CSV → FIFO 配对落库，不过入场闸门）")
    p_import.add_argument("--file", required=True, help="成交明细文件路径（.md/.csv）")
    p_import.add_argument("--year", type=int, default=datetime.now().year, help="日期补全年份（默认当前年）")
    p_import.add_argument("--symbol-map", default="", dest="symbol_map",
                          help="名称=代码 手动映射，逗号分隔（如 通源石油=300164,壹连科技=301631）")
    p_import.add_argument("--account", default="事件", help="账户类型（默认 事件）")
    p_import.add_argument("--system", default="", help="入场系统（默认空，导入历史多为非系统交易）")
    p_import.add_argument("--dry-run", action="store_true", help="只打印配对结果，不落库")
    p_import.set_defaults(func=cmd_import)

    p_pos = sub.add_parser("positions", help="持仓摘要（未实现盈亏/风险敞口/回撤状态）")
    p_pos.add_argument("--equity", type=float, default=None, help="账户权益")
    p_pos.set_defaults(func=cmd_positions)

    p_sell = sub.add_parser("sell-check", help="卖点检查单（止损/退出通道/持有天数/热点池/建议挂单价）")
    p_sell.add_argument("symbol", help="股票代码")
    p_sell.set_defaults(func=cmd_sell_check)

    p_addpos = sub.add_parser("add-position", help="金字塔加仓（V5.0 §5.5：0.5N 触发判定 → 落库 → 全链止损上移）")
    p_addpos.add_argument("symbol", help="股票代码")
    p_addpos.add_argument("--price", type=float, default=None,
                          help="实际成交价（默认 market.db 最新收盘价）")
    p_addpos.add_argument("--name", default="", help="股票名称（默认沿首仓）")
    p_addpos.add_argument("--equity", type=float, default=None, help="账户权益（默认 config.ACCOUNT_EQUITY）")
    p_addpos.add_argument("--force", action="store_true", help="高级违规也强制写入（备注留痕）")
    p_addpos.set_defaults(func=cmd_add_position)

    p_sell_reg = sub.add_parser("sell", help="卖出登记（全平闭环 / 部分卖出拆单，R 自动结算）")
    p_sell_reg.add_argument("symbol", help="股票代码")
    p_sell_reg.add_argument("--shares", type=int, required=True, help="卖出股数（≥ 持仓股数即全平）")
    p_sell_reg.add_argument("--price", type=float, required=True, help="卖出价")
    p_sell_reg.add_argument("--id", default=None, help="指定交易编号（默认该代码最新一笔持仓）")
    p_sell_reg.add_argument("--date", default=None, help="卖出日期 YYYY-MM-DD（默认今天）")
    p_sell_reg.add_argument("--time", default="", help="卖出时间 HH:MM:SS（可选，纪律审计用）")
    p_sell_reg.add_argument("--reason", default="", help="退出原因（部分卖出默认「分批止盈」）")
    p_sell_reg.add_argument("--equity", type=float, default=None, help="账户权益（默认 config.ACCOUNT_EQUITY）")
    p_sell_reg.set_defaults(func=cmd_sell)

    p_scan = sub.add_parser("from-scan", help="从扫描 JSON 读取突破数据（--execute 一键建仓落库）")
    p_scan.add_argument("symbol", help="股票代码")
    p_scan.add_argument("--date", default=None, help="扫描日期 YYYY-MM-DD（默认今天）")
    p_scan.add_argument("--execute", action="store_true", help="按信号建仓落库（含合规闸门与买入卡）")
    p_scan.add_argument("--system", choices=["S1-A", "S2-A", "HOT-S"], default=None,
                        help="入场系统（默认：仅单一信号用该信号；同股双信号按 S2-A；仅热点信号按 HOT-S）")
    p_scan.add_argument("--account", default=None, help="账户类型（默认按系统映射：S1→产业、S2→核心、HOT-S→事件）")
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
