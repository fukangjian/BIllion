"""
情绪周期状态机 — 游资「冰点/修复/发酵/高潮/退潮」五相位的量化判定（2026-09）

与 market_state（指数/量能/宽度 → A/B/C/D，indicators.judge_market_state）互补：
本口径只描述超短打板生态，输入全部来自本地 DB（limit_stats / limit_pool）：

- 涨停家数 / 跌停家数 / 炸板数：limit_stats 当日行
- 炸板率 = 炸板 /（涨停 + 炸板）
- 连板高度（max_lbc）/ 连板家数：limit_pool 当日 pool_type=up
- 晋级率 = 今日连板家数 / 昨日涨停家数（昨日涨停今日再封板比例）
- 相对前日变化：涨停家数增减、连板高度回落（读前一个有池数据的交易日）

判定为各相位投票制（阈值 config.SENTIMENT_*），平票按「退潮>冰点>高潮>发酵>修复」
保守优先。相位驱动：HOT-S/二板/EVT-S 开仓闸门（compliance_check）、仓位乘数
（daily_plan）、竞价收紧（auction_check）。全链路优雅降级：数据缺失 → 相位「未知」，
不触发任何门禁、不阻塞链路。

用法:
    python pipeline/sentiment_regime.py run                 # 用本地 DB 重算并写入当日相位
    python pipeline/sentiment_regime.py backfill --days 28 # 联网回填历史（东财池接口仅支持最近约 30 自然日）
    python pipeline/sentiment_regime.py show               # 打印最近 10 个交易日相位
"""
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import argparse
import logging
from datetime import timedelta

import pandas as pd

from config import (
    SENTIMENT_BAN_PHASES,
    SENTIMENT_BROKEN_RATE_EBB,
    SENTIMENT_BROKEN_RATE_ICEPOINT,
    SENTIMENT_BROKEN_RATE_SAFE,
    SENTIMENT_LIMIT_DOWN_ICEPOINT,
    SENTIMENT_LIMIT_UP_CLIMAX,
    SENTIMENT_LIMIT_UP_DROP_EBB,
    SENTIMENT_LIMIT_UP_ICEPOINT,
    SENTIMENT_MAX_LBC_CLIMAX,
    SENTIMENT_MAX_LBC_FERMENT,
    SENTIMENT_PHASES,
    SENTIMENT_PROMOTION_EBB,
    SENTIMENT_PROMOTION_FERMENT,
    SENTIMENT_RISK_MULT,
    SENTIMENT_UNKNOWN,
    SENTIMENT_AUCTION_TIGHTEN,
)

logger = logging.getLogger(__name__)

# 平票保守优先序：风险相位先于机会相位
_TIE_BREAK = ("退潮", "冰点", "高潮", "发酵", "修复")

_PHASE_ADVICE = {
    "冰点": "禁开新仓；只观察首板质量与板块异动，冰点后首日回暖是最佳买点",
    "修复": "超短风险减半试错，只做最强主线龙头，不加仓跟风",
    "发酵": "正常开仓：龙头/二板可上标准仓位，聚焦晋级成功的方向",
    "高潮": "只持有不加仓，挂移动止盈；警惕高位股分歧转一致失败",
    "退潮": "禁开新仓；次日溢价预期下调，持仓竞价风控收紧，等情绪重新冰点企稳",
    SENTIMENT_UNKNOWN: "情绪数据缺失，按原有口径执行（不触发情绪门禁）",
}


# ---------- 纯函数：指标与相位 ----------

def _f(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def _i(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def metrics_from_rows(
    stats_row: Optional[dict],
    pool_rows_today: list[dict],
    pool_rows_prev: list[dict],
) -> Optional[dict]:
    """
    由 limit_stats 当日行 + limit_pool 当日/前日行计算情绪指标（纯函数，可离线单测）。

    stats_row：limit_stats 当日行 dict（可为 None，跌停数缺失时降级）；
    pool_rows_*：limit_pool 全量行（含 pool_type 列）。
    数据无效（当日涨停池为空且 stats 无涨停数）返回 None → 相位「未知」。
    """
    up_today = [r for r in pool_rows_today if str(r.get("pool_type", "")) == "up"]
    broken_today = [r for r in pool_rows_today if str(r.get("pool_type", "")) == "broken"]
    stats_row = stats_row or {}

    limit_up = len(up_today) or _i(stats_row.get("limit_up_count"))
    broken = len(broken_today) or _i(stats_row.get("broken_count"))
    limit_down = _i(stats_row.get("limit_down_count"))
    # 有效性：涨停池与 stats 双缺（抓取失败日会存 0）视为数据缺失，不误判冰点
    if limit_up <= 0:
        return None

    lbcs = [_i(r.get("lbc"), 1) for r in up_today]
    max_lbc = max(lbcs) if lbcs else 0
    lianban = sum(1 for v in lbcs if v >= 2)

    up_prev = [r for r in pool_rows_prev if str(r.get("pool_type", "")) == "up"]
    prev_limit_up = len(up_prev)
    # 池明细缺失（仅 stats 兜底）时连板/晋级指标不可算：置 None 不投票，避免「数据缺失≠晋级率低」误判退潮
    has_pool = bool(up_today)
    promotion = (
        round(lianban / prev_limit_up * 100, 1)
        if has_pool and prev_limit_up > 0 else None
    )
    broken_rate = round(broken / (limit_up + broken) * 100, 1) if (limit_up + broken) > 0 else None

    return {
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "broken_count": broken,
        "broken_rate": broken_rate,
        "max_lbc": max_lbc,
        "lianban_count": lianban,
        "promotion_rate": promotion,
        "prev_limit_up_count": prev_limit_up,
        "pool_detail_missing": not has_pool,
    }


def classify_phase(
    m: Optional[dict],
    prev_phase: Optional[str] = None,
) -> tuple[str, dict, list[str]]:
    """
    情绪相位投票判定（纯函数）。返回 (相位, 各相位票数, 当选相位触发原因列表)。
    m 为 None → (未知, {}, [])。平票按保守优先序 _TIE_BREAK。
    """
    if not m:
        return SENTIMENT_UNKNOWN, {}, []

    votes = {p: 0 for p in SENTIMENT_PHASES}
    reasons: dict[str, list[str]] = {p: [] for p in SENTIMENT_PHASES}

    lu = m["limit_up_count"]
    ld = m["limit_down_count"]
    br = m.get("broken_rate")
    mlbc = m.get("max_lbc", 0)
    promo = m.get("promotion_rate")
    prev_lu = m.get("prev_limit_up_count") or 0

    # 冰点：市场极度悲观
    if lu < SENTIMENT_LIMIT_UP_ICEPOINT:
        votes["冰点"] += 2
        reasons["冰点"].append(f"涨停 {lu} 家 < {SENTIMENT_LIMIT_UP_ICEPOINT}")
    if ld > SENTIMENT_LIMIT_DOWN_ICEPOINT:
        votes["冰点"] += 2
        reasons["冰点"].append(f"跌停 {ld} 家 > {SENTIMENT_LIMIT_DOWN_ICEPOINT}")
    if br is not None and br >= SENTIMENT_BROKEN_RATE_ICEPOINT:
        votes["冰点"] += 2
        reasons["冰点"].append(f"炸板率 {br}% ≥ {SENTIMENT_BROKEN_RATE_ICEPOINT:.0f}%")
    if 0 < mlbc <= 3:
        votes["冰点"] += 1
        reasons["冰点"].append(f"连板高度仅 {mlbc} 板")

    # 退潮：亏钱效应扩散
    if br is not None and br >= SENTIMENT_BROKEN_RATE_EBB:
        votes["退潮"] += 2
        reasons["退潮"].append(f"炸板率 {br}% ≥ {SENTIMENT_BROKEN_RATE_EBB:.0f}%")
    if promo is not None and promo < SENTIMENT_PROMOTION_EBB:
        votes["退潮"] += 2
        reasons["退潮"].append(f"晋级率 {promo}% < {SENTIMENT_PROMOTION_EBB:.0f}%")
    if prev_lu > 0:
        drop = (prev_lu - lu) / prev_lu * 100
        if drop >= SENTIMENT_LIMIT_UP_DROP_EBB:
            votes["退潮"] += 2
            reasons["退潮"].append(f"涨停较前日 -{drop:.0f}%（{prev_lu}→{lu}）")
        elif lu > prev_lu:
            votes["发酵"] += 1
            reasons["发酵"].append(f"涨停较前日增加（{prev_lu}→{lu}）")
    if mlbc >= SENTIMENT_MAX_LBC_FERMENT:
        votes["发酵"] += 1
        reasons["发酵"].append(f"连板高度 {mlbc} 板 ≥ {SENTIMENT_MAX_LBC_FERMENT}")

    # 高潮：极度亢奋
    if lu >= SENTIMENT_LIMIT_UP_CLIMAX:
        votes["高潮"] += 2
        reasons["高潮"].append(f"涨停 {lu} 家 ≥ {SENTIMENT_LIMIT_UP_CLIMAX}")
    if mlbc >= SENTIMENT_MAX_LBC_CLIMAX:
        votes["高潮"] += 2
        reasons["高潮"].append(f"连板高度 {mlbc} 板 ≥ {SENTIMENT_MAX_LBC_CLIMAX}")
    if br is not None and br < 25:
        votes["高潮"] += 1
        reasons["高潮"].append(f"炸板率仅 {br}%")

    # 发酵：赚钱效应扩散
    if SENTIMENT_LIMIT_UP_ICEPOINT <= lu < SENTIMENT_LIMIT_UP_CLIMAX:
        votes["发酵"] += 1
    if promo is not None and promo >= SENTIMENT_PROMOTION_FERMENT:
        votes["发酵"] += 2
        reasons["发酵"].append(f"晋级率 {promo}% ≥ {SENTIMENT_PROMOTION_FERMENT:.0f}%")
    if br is not None and br < SENTIMENT_BROKEN_RATE_SAFE:
        votes["发酵"] += 1

    # 修复：冰点后回暖（转折日），或中性偏暖的默认相位
    if prev_phase == "冰点":
        if prev_lu > 0 and lu > prev_lu:
            votes["修复"] += 3
            reasons["修复"].append(f"冰点后涨停回暖（{prev_lu}→{lu}）")
        if 0 < ld < SENTIMENT_LIMIT_DOWN_ICEPOINT:
            votes["修复"] += 1
            reasons["修复"].append(f"跌停收敛至 {ld} 家")
        if promo is not None and promo >= 20:
            votes["修复"] += 1
            reasons["修复"].append(f"晋级率回升 {promo}%")
    if SENTIMENT_LIMIT_UP_ICEPOINT <= lu < SENTIMENT_LIMIT_UP_CLIMAX and mlbc <= 3 \
            and (br is None or br < SENTIMENT_BROKEN_RATE_SAFE):
        votes["修复"] += 1
        reasons["修复"].append("中性偏暖（涨停居中/高度低/炸板率低）")

    phase = max(_TIE_BREAK, key=lambda p: votes[p])
    if votes[phase] <= 0:
        phase = "修复"
        reasons[phase] = ["无显著信号，按中性修复处理"]
    return phase, votes, reasons.get(phase, [])


def risk_multiplier(phase: Optional[str]) -> float:
    """相位 → 超短单笔风险乘数（未知/None → 1.0，不干预）"""
    if not phase or phase == SENTIMENT_UNKNOWN:
        return 1.0
    return SENTIMENT_RISK_MULT.get(phase, 1.0)


def is_banned(phase: Optional[str]) -> bool:
    """冰点/退潮 → 禁开新超短仓"""
    return bool(phase) and phase in SENTIMENT_BAN_PHASES


def tightened_auction_params(phase: Optional[str]) -> Optional[dict]:
    """退潮/冰点相位下的竞价收紧口径（None = 不收紧，用 AUCTION_* 默认）"""
    if phase in SENTIMENT_BAN_PHASES:
        return dict(SENTIMENT_AUCTION_TIGHTEN)
    return None


def phase_advice(phase: Optional[str]) -> str:
    return _PHASE_ADVICE.get(phase or SENTIMENT_UNKNOWN, _PHASE_ADVICE[SENTIMENT_UNKNOWN])


# ---------- DB 读写与编排（优雅降级，不抛异常） ----------

def _load_stats_row(trade_date: str, db_path: Optional[Path] = None) -> Optional[dict]:
    try:
        from pipeline.database import get_connection

        with get_connection(db_path) as conn:
            cur = conn.execute(
                "SELECT * FROM limit_stats WHERE trade_date = ?", (trade_date,)
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    except Exception as e:
        logger.warning("limit_stats 读取失败 %s: %s", trade_date, e)
        return None


def _prev_pool_date(trade_date: str, db_path: Optional[Path] = None) -> Optional[str]:
    try:
        from pipeline.database import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) FROM limit_pool WHERE trade_date < ? AND pool_type = 'up'",
                (trade_date,),
            ).fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        logger.warning("limit_pool 前一交易日查询失败: %s", e)
        return None


def _latest_pool_date_on_or_before(trade_date: str, db_path: Optional[Path] = None) -> Optional[str]:
    """<= trade_date 的最近一期池数据日（盘前运行时当日池未生成，回退用）"""
    try:
        from pipeline.database import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) FROM limit_pool WHERE trade_date <= ?",
                (trade_date,),
            ).fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        logger.warning("limit_pool 最近数据日查询失败: %s", e)
        return None


def _load_pool_rows(trade_date: str, db_path: Optional[Path] = None) -> list[dict]:
    try:
        from pipeline.database import load_limit_pool

        df: pd.DataFrame = load_limit_pool(trade_date=trade_date, db_path=db_path)
        return df.to_dict("records") if not df.empty else []
    except Exception as e:
        logger.warning("limit_pool 读取失败 %s: %s", trade_date, e)
        return []


def _load_prev_phase(trade_date: str, db_path: Optional[Path] = None) -> Optional[str]:
    try:
        from pipeline.database import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT phase FROM emotion_state WHERE trade_date < ? "
                "AND phase IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
                (trade_date,),
            ).fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        logger.warning("emotion_state 前一相位读取失败: %s", e)
        return None


def compute_and_save(trade_date: Optional[str] = None, db_path: Optional[Path] = None) -> Optional[dict]:
    """
    用本地 DB 计算情绪相位并写入 emotion_state 表（行主键 = 数据实际所属交易日）。

    盘前 08:30 运行时当日 limit_pool 尚未生成（东财池标注上一交易日），
    自动回退到 <= trade_date 的最近一期池数据并按该数据日落库——
    相位始终描述「最近已收盘交易日」的情绪，供次日开仓闸门与竞价收紧消费。
    数据无效（池与 stats 双缺）不写库，返回 None。
    """
    from pipeline.database import init_database, save_emotion_state

    init_database(db_path)
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")

    stats_row = _load_stats_row(trade_date, db_path)
    pool_today = _load_pool_rows(trade_date, db_path)
    data_date = trade_date
    if not pool_today and not (stats_row and _i(stats_row.get("limit_up_count")) > 0):
        latest = _latest_pool_date_on_or_before(trade_date, db_path)
        if latest and latest < trade_date:
            data_date = latest
            pool_today = _load_pool_rows(data_date, db_path)
            stats_row = _load_stats_row(data_date, db_path)

    prev_date = _prev_pool_date(data_date, db_path)
    pool_prev = _load_pool_rows(prev_date, db_path) if prev_date else []
    m = metrics_from_rows(stats_row, pool_today, pool_prev)
    if m is None:
        logger.info("情绪指标数据缺失 %s（涨停池/统计双缺），相位判为未知", trade_date)
        return None

    prev_phase = _load_prev_phase(data_date, db_path)
    phase, votes, reasons = classify_phase(m, prev_phase)

    row = {
        "trade_date": data_date,
        "phase": phase,
        "limit_up_count": m["limit_up_count"],
        "limit_down_count": m["limit_down_count"],
        "broken_count": m["broken_count"],
        "broken_rate": m.get("broken_rate"),
        "max_lbc": m.get("max_lbc"),
        "lianban_count": m.get("lianban_count"),
        "promotion_rate": m.get("promotion_rate"),
        "prev_phase": prev_phase,
        "notes": "；".join(reasons),
    }
    try:
        save_emotion_state(row, db_path=db_path)
        logger.info("情绪相位写入 %s: %s（%s）", data_date, phase, row["notes"])
    except Exception as e:
        logger.warning("emotion_state 写入失败（不阻塞链路）: %s", e)
    row["advice"] = phase_advice(phase)
    row["votes"] = votes
    return row


def get_latest_phase(db_path: Optional[Path] = None) -> Optional[dict]:
    """最新已判定相位（emotion_state 表；无数据/读取失败返回 None，调用方降级跳过）"""
    try:
        from pipeline.database import load_emotion_state

        df = load_emotion_state(db_path=db_path)
        if df.empty:
            return None
        r = df.iloc[0].to_dict()
        if not r.get("phase"):
            return None
        r["advice"] = phase_advice(r["phase"])
        r["risk_mult"] = risk_multiplier(r["phase"])
        r["trading_allowed"] = not is_banned(r["phase"])
        return r
    except Exception as e:
        logger.warning("emotion_state 读取失败（情绪闸门降级跳过）: %s", e)
        return None


def current_context(db_path: Optional[Path] = None) -> Optional[dict]:
    """扫描/计划层消费的完整情绪上下文（无数据返回 None，调用方按「未知」展示）"""
    r = get_latest_phase(db_path)
    if not r:
        return None
    return {
        "date": r.get("trade_date"),
        "phase": r.get("phase"),
        "prev_phase": r.get("prev_phase"),
        "metrics": {
            "limit_up": r.get("limit_up_count"),
            "limit_down": r.get("limit_down_count"),
            "broken": r.get("broken_count"),
            "broken_rate": r.get("broken_rate"),
            "max_lbc": r.get("max_lbc"),
            "lianban": r.get("lianban_count"),
            "promotion_rate": r.get("promotion_rate"),
        },
        "notes": r.get("notes", ""),
        "advice": r.get("advice", ""),
        "risk_mult": r.get("risk_mult"),
        "trading_allowed": r.get("trading_allowed"),
    }


def backfill(days: int = 28, db_path: Optional[Path] = None) -> int:
    """
    联网回填历史情绪数据：逐日抓 limit_stats + limit_pool 入库（库里已有池数据的日期跳过抓取），
    再对窗口内所有有数据的交易日（含既往每日管道写入的池数据）按日期升序重算相位。
    非交易日/抓取失败自动跳过；当天（盘中未收盘）不抓不判。返回成功判定相位的交易日数。

    注意：东财涨停/炸板池接口只支持最近约 30 个自然日（akshare 实测限制），
    days 超过 30 的部分会静默跳过（无数据），勿设过大浪费重试时间。
    """
    from pipeline.database import get_connection, save_limit_pool, save_limit_stats

    today = datetime.now()
    today_ds = today.strftime("%Y-%m-%d")
    start_ds = (today - timedelta(days=days)).strftime("%Y-%m-%d")

    # 窗口内库里已有池数据的日期：跳过抓取（每日管道已写入，避免重复请求与限流）
    existing_pool_dates: set[str] = set()
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM limit_pool WHERE trade_date >= ? AND trade_date <= ?",
                (start_ds, today_ds),
            ).fetchall()
        existing_pool_dates = {r[0] for r in rows}
    except Exception as e:
        logger.warning("既有池数据日期查询失败（逐日全量抓取）: %s", e)

    fetched: list[str] = []
    for k in range(days, -1, -1):
        ds = (today - timedelta(days=k)).strftime("%Y-%m-%d")
        if ds >= today_ds:
            continue  # 当天数据未定稿（盘中/盘前），不抓不判
        if ds in existing_pool_dates:
            continue
        try:
            from shared.data_fetcher import fetch_limit_pools, fetch_limit_stats

            stats_df = fetch_limit_stats(trade_date=ds)
            pools_df = fetch_limit_pools(trade_date=ds)
        except Exception as e:
            logger.warning("回填抓取失败 %s（跳过）: %s", ds, e)
            continue
        has_stats = stats_df is not None and not stats_df.empty
        has_pools = pools_df is not None and not pools_df.empty
        if not (has_stats or has_pools):
            continue  # 非交易日或无数据
        try:
            if has_stats:
                save_limit_stats(stats_df, db_path=db_path)
            if has_pools:
                save_limit_pool(pools_df, db_path=db_path)
            fetched.append(ds)
        except Exception as e:
            logger.warning("回填入库失败 %s（跳过）: %s", ds, e)

    # 相位重算覆盖窗口内全部有数据交易日（库中既有 + 本次新抓）
    all_dates = sorted(existing_pool_dates | set(fetched))
    all_dates = [d for d in all_dates if start_ds <= d < today_ds]
    written = 0
    for ds in all_dates:
        row = compute_and_save(ds, db_path=db_path)
        if row:
            written += 1
    logger.info("情绪周期回填完成：%d 个交易日（库中既有 %d + 本次新抓 %d）",
                written, len(existing_pool_dates), len(fetched))
    return written


# ---------- CLI ----------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="情绪周期状态机 — 冰点/修复/发酵/高潮/退潮判定")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="用本地 DB 重算并写入当日相位")
    p_back = sub.add_parser("backfill", help="联网回填历史 limit_stats/limit_pool 并逐日判相位")
    p_back.add_argument("--days", type=int, default=90, help="回填自然日数（默认 90）")
    sub.add_parser("show", help="打印最近相位")

    args = parser.parse_args()
    if args.command == "backfill":
        n = backfill(days=args.days)
        print(f"回填完成：{n} 个交易日已判定相位")
        return
    if args.command == "show":
        ctx = current_context()
        if not ctx:
            print("无情绪相位数据（先 run 或 backfill）")
            return
        m = ctx["metrics"]
        print(f"=== 情绪周期（{ctx['date']}）：{ctx['phase']}（前日 {ctx.get('prev_phase') or '—'}）===")
        print(f"涨停 {m['limit_up']} / 跌停 {m['limit_down']} / 炸板 {m['broken']}"
              f"（炸板率 {m['broken_rate']}%）/ 最高连板 {m['max_lbc']} 板 / "
              f"连板 {m['lianban']} 家 / 晋级率 {m['promotion_rate']}%")
        print(f"触发：{ctx['notes']}")
        print(f"操作：{ctx['advice']}")
        return
    row = compute_and_save()
    if not row:
        print("当日情绪数据缺失（涨停池/统计双缺），相位未知")
        return
    print(f"{row['trade_date']} 情绪相位：{row['phase']}（{row['notes']}）")


if __name__ == "__main__":
    main()
