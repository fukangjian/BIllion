"""
报告生成 — 周报、月报 Markdown 输出到 Obsidian 知识库
"""
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from config import MONTHLY_OUTPUT_DIR, WEEKLY_OUTPUT_DIR
from review.compliance_check import run_compliance_check, report_to_markdown
from review.metrics import (
    compute_stats,
    enrich_trade_metrics,
    group_by_account,
    group_by_strategy,
    stats_to_markdown,
)
from review.trade_log import Trade, TradeLog

logger = logging.getLogger(__name__)


def _obsidian_frontmatter(tags: list[str], **extra) -> str:
    lines = ["---"]
    lines.append(f"tags: [{', '.join(tags)}]")
    lines.append(f"created: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    for k, v in extra.items():
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def _df_to_md(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_暂无数据_"
    headers = "| " + " | ".join(str(c) for c in df.columns) + " |"
    sep = "| " + " | ".join("---" for _ in df.columns) + " |"
    rows = ["| " + " | ".join(str(v) for v in row.values) + " |" for _, row in df.iterrows()]
    return "\n".join([headers, sep] + rows)


def _trade_detail_table(trades: list[Trade]) -> str:
    if not trades:
        return "_本周/月无交易_"

    rows = []
    for t in trades:
        r = t.R倍数
        if r is None and t.is_closed:
            from review.metrics import calc_r_multiple
            r = calc_r_multiple(t.入场价, t.实际退出价, t.止损价)
        rows.append({
            "编号": t.交易编号[-8:],
            "日期": t.日期,
            "代码": t.股票代码,
            "名称": t.股票名称,
            "系统": t.入场系统,
            "R": f"{r:.2f}" if r is not None else "-",
            "MFE": f"{t.MFE:.2f}" if t.MFE is not None else "-",
            "MAE": f"{t.MAE:.2f}" if t.MAE is not None else "-",
            "系统内": "是" if t.是否系统内交易 else "否",
            "状态": "已平" if t.is_closed else "持仓",
        })
    return _df_to_md(pd.DataFrame(rows))


def _get_week_range(ref_date: datetime | None = None) -> tuple[str, str, str]:
    ref = ref_date or datetime.now()
    start = ref - timedelta(days=ref.weekday())
    end = start + timedelta(days=6)
    week_id = start.strftime("%Y-W%W")
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), week_id


def _get_month_range(ref_date: datetime | None = None) -> tuple[str, str, str]:
    ref = ref_date or datetime.now()
    start = ref.replace(day=1)
    if ref.month == 12:
        end = ref.replace(year=ref.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        end = ref.replace(month=ref.month + 1, day=1) - timedelta(days=1)
    month_id = ref.strftime("%Y-%m")
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), month_id


def signal_verification_section(days: int = 90) -> str:
    """信号验证节内容（周报/月报/统计快照共用）；信号统计失败时降级标注"""
    try:
        from pipeline.signal_tracker import signal_stats, signal_stats_to_markdown

        return signal_stats_to_markdown(signal_stats(days=days))
    except Exception as e:
        logger.warning("信号统计不可用（已降级）: %s", e)
        return f"_信号统计不可用（已降级，详见日志）: {e}_"


def discipline_audit_section(trades: list[Trade]) -> str:
    """纪律审计节内容（周报/月报共用，审计范围为该周期 trades）；审计失败时降级标注"""
    try:
        from review.discipline_audit import audit_discipline, audit_to_markdown

        return audit_to_markdown(audit_discipline(trades))
    except Exception as e:
        logger.warning("纪律审计不可用（已降级）: %s", e)
        return f"_纪律审计不可用: {e}_"


def generate_weekly_report(
    trades: list[Trade] | None = None,
    ref_date: datetime | None = None,
    output_dir: Path | None = None,
    fetch_prices: bool = False,
) -> Path:
    start, end, week_id = _get_week_range(ref_date)
    output_dir = output_dir or WEEKLY_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    if trades is None:
        log = TradeLog()
        trades = log.list_by_date_range(start, end)

    closed = [t for t in trades if t.is_closed]
    if fetch_prices:
        for t in closed:
            enrich_trade_metrics(t, fetch_prices=True)

    stats = compute_stats(trades, fetch_prices=False)
    strategy_df = group_by_strategy(trades)
    compliance = run_compliance_check(trades)

    lines = [
        _obsidian_frontmatter(
            ["周报", "交易复盘"],
            week=week_id,
            period=f"{start} ~ {end}",
        ),
        f"# 交易周报 · {week_id}",
        "",
        f"> 周期: {start} ~ {end}",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 一、关键指标",
        "",
        stats_to_markdown(stats),
        "",
        "## 二、策略分组",
        "",
        _df_to_md(strategy_df),
        "",
        "## 三、交易明细",
        "",
        _trade_detail_table(trades),
        "",
        "## 四、合规检查",
        "",
        report_to_markdown(compliance),
        "",
        "## 五、纪律审计",
        "",
        discipline_audit_section(trades),
        "",
        "## 六、信号验证（近 90 天）",
        "",
        signal_verification_section(),
        "",
        "## 七、本周反思",
        "",
        "### 最大错误",
        "> （待填写）",
        "",
        "### 最佳执行",
        "> （待填写）",
        "",
        "### 下周改进",
        "> （待填写）",
        "",
    ]

    output_path = output_dir / f"{week_id}_交易周报.md"
    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("周报已生成: %s", output_path)
    return output_path


def generate_monthly_report(
    trades: list[Trade] | None = None,
    ref_date: datetime | None = None,
    output_dir: Path | None = None,
    fetch_prices: bool = False,
) -> Path:
    start, end, month_id = _get_month_range(ref_date)
    output_dir = output_dir or MONTHLY_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    if trades is None:
        log = TradeLog()
        trades = log.list_by_date_range(start, end)

    closed = [t for t in trades if t.is_closed]
    if fetch_prices:
        for t in closed:
            enrich_trade_metrics(t, fetch_prices=True)

    stats = compute_stats(trades, fetch_prices=False)
    strategy_df = group_by_strategy(trades)
    account_df = group_by_account(trades)
    compliance = run_compliance_check(trades)

    health_items = []
    if stats.系统内占比 >= 90:
        health_items.append("[OK] 系统内交易占比 >= 90%")
    else:
        health_items.append(f"[WARN] 系统内交易占比 {stats.系统内占比}%，目标 >= 90%")

    if stats.期望值 > 0:
        health_items.append(f"[OK] 期望值为正 ({stats.期望值}R)")
    else:
        health_items.append(f"[WARN] 期望值为 {stats.期望值}R")

    pf = stats.profit_factor
    if pf > 1.3:
        health_items.append(f"[OK] Profit Factor = {pf:.2f}")
    elif pf != float("inf"):
        health_items.append(f"[WARN] Profit Factor = {pf:.2f}，目标 > 1.3")

    if compliance.合规率 >= 95:
        health_items.append(f"[OK] 合规率 {compliance.合规率}%")
    else:
        health_items.append(f"[WARN] 合规率 {compliance.合规率}%")

    lines = [
        _obsidian_frontmatter(
            ["月报", "系统健康", "交易复盘"],
            month=month_id,
            period=f"{start} ~ {end}",
        ),
        f"# 月度系统健康报告 · {month_id}",
        "",
        f"> 周期: {start} ~ {end}",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 一、系统健康概览",
        "",
    ]
    lines.extend(health_items)
    lines.extend([
        "",
        "## 二、累计指标",
        "",
        stats_to_markdown(stats),
        "",
        "## 三、策略分析",
        "",
        _df_to_md(strategy_df),
        "",
        "## 四、账户分析",
        "",
        _df_to_md(account_df),
        "",
        "## 五、交易明细",
        "",
        _trade_detail_table(trades),
        "",
        "## 六、合规与违规",
        "",
        report_to_markdown(compliance),
        "",
        "## 七、纪律审计",
        "",
        discipline_audit_section(trades),
        "",
        "## 八、信号验证（近 90 天）",
        "",
        signal_verification_section(),
        "",
        "## 九、改进方向",
        "",
        "### 本月亮点",
        "> （待填写）",
        "",
        "### 待改进项",
        "> （待填写）",
        "",
        "### 下月参数调整",
        "> （每季度最多一次正式版本升级）",
        "",
    ])

    output_path = output_dir / f"{month_id}_系统健康报告.md"
    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("月报已生成: %s", output_path)
    return output_path
