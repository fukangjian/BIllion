"""
二板战法观察池 — vault《二板打法.md》「首板识别 → 二板买入」框架的量化落地

每日扫描时从 limit_pool 表（run_all 取数阶段写入）筛首板股（涨停且连板数 ≤ 1）：

- 硬性条件（全部满足才进池；数据缺失视为不满足，宁缺毋滥）：
  首封时间（主板 <10:00，科创板 <9:45）、封单金额 ≥ 流通市值 × 3%、换手率 <12%、
  流通市值 30-120 亿、股价 10-60 元、首板前 5 日涨幅 <15%、非 ST/北交所/N/C 字头新股；
  「无当日利空」无离线数据源，不自动判定，需人工核对。
- 软性评分（≥ SECOND_BOARD_MIN_SCORE 进池，按评分降序取前 SECOND_BOARD_TOP_N）：
  板块内首封时间第 1（+2）/前 3（+1）、突破前高平台（+2）、龙虎榜净买入（+1，
  机构/游资席位构成需人工核对）、名称带热点关键词（+1）、流通市值 <50 亿（+1）、
  涨停价为整数关口（+1）；研报/互动易利好无离线数据源，不自动评分。
- 每只候选附带次日计划参数：次日涨停价（S 级竞价挂单参考）与 -10% 硬止损位。

纯函数 screen_first_boards 可离线单测；build_second_board_pool 仅读本地 DB。
次日竞价 S/A/B/C 等级判定与开盘处理依赖盘中数据，由 Web 控制台静态清单提示，人工执行。

用法:
    python pipeline/second_board.py            # 打印今日二板观察池（离线）
"""
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging

import pandas as pd

from config import (
    SECOND_BOARD_CAP_MAX_YI,
    SECOND_BOARD_CAP_MIN_YI,
    SECOND_BOARD_HOT_KEYWORDS,
    SECOND_BOARD_MIN_SCORE,
    SECOND_BOARD_PREV5_GAIN_MAX,
    SECOND_BOARD_PRICE_MAX,
    SECOND_BOARD_PRICE_MIN,
    SECOND_BOARD_SEAL_RATIO_MIN,
    SECOND_BOARD_SEAL_TIME_MAIN,
    SECOND_BOARD_SEAL_TIME_STAR,
    SECOND_BOARD_SMALL_CAP_YI,
    SECOND_BOARD_STOP_PCT,
    SECOND_BOARD_TOP_N,
    SECOND_BOARD_TURNOVER_MAX,
)

logger = logging.getLogger(__name__)

_BOARD_NAMES = {"STAR": "科创板", "GEM": "创业板", "MAIN": "主板", "BSE": "北交所"}


def _to_float(v, default: float = 0.0) -> float:
    """NaN 安全 float 转换"""
    try:
        f = float(v)
        return default if f != f else f  # f != f 即 NaN
    except (TypeError, ValueError):
        return default


def board_of(symbol: str) -> str:
    """板块归属：STAR=科创板(688) / GEM=创业板(300/301/302) / BSE=北交所 / MAIN=主板"""
    s = str(symbol).zfill(6)[-6:]
    if s.startswith("688"):
        return "STAR"
    if s.startswith(("300", "301", "302")):
        return "GEM"
    if s.startswith(("8", "4", "920")):
        return "BSE"
    return "MAIN"


def limit_up_pct(symbol: str) -> float:
    """涨跌停幅度：科创板/创业板 20%，主板 10%"""
    return 0.20 if board_of(symbol) in ("STAR", "GEM") else 0.10


def norm_fbt(v) -> str:
    """首次封板时间归一化为 HHMMSS 六位字符串（空/非法 → ""）"""
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


def fmt_fbt(fbt: str) -> str:
    """HHMMSS → HH:MM:SS 显示用"""
    return f"{fbt[:2]}:{fbt[2:4]}:{fbt[4:]}" if len(fbt) == 6 else (fbt or "-")


def prev5_gain(df: Optional[pd.DataFrame]) -> Optional[float]:
    """首板前 5 日涨幅（%）：昨日收盘 / 其 5 个交易日前收盘 − 1（不含首板当日）；数据不足返回 None"""
    if df is None or df.empty or "close" not in df.columns:
        return None
    closes = pd.to_numeric(df.sort_values("trade_date")["close"], errors="coerce").dropna()
    prior = closes.iloc[:-1]  # 剔除首板当日
    if len(prior) < 5:
        return None
    base = float(prior.iloc[-5])
    if base <= 0:
        return None
    return round((float(prior.iloc[-1]) / base - 1) * 100, 2)


def broke_prior_high(df: Optional[pd.DataFrame], min_bars: int = 20) -> bool:
    """首板收盘价突破前高/平台：收盘 > 此前全部交易日最高价（数据窗口内，不足 min_bars 根不判）"""
    if df is None or df.empty or not {"close", "high"} <= set(df.columns):
        return False
    d = df.sort_values("trade_date")
    prior_high = pd.to_numeric(d["high"].iloc[:-1], errors="coerce").dropna()
    if len(prior_high) < min_bars:
        return False
    close = _to_float(d["close"].iloc[-1], default=float("nan"))
    return close == close and close > float(prior_high.max())


def hard_filter_reasons(rec: dict, daily: Optional[pd.DataFrame]) -> tuple[list[str], dict]:
    """
    硬性条件逐条核对，返回 (未通过原因列表, 计算过程值)。
    过程值含 price/prev5/seal_ratio/cap_yi，供评分与输出复用；原因为空即硬条件全过。
    """
    reasons: list[str] = []
    symbol = str(rec.get("symbol", "")).zfill(6)[-6:]
    name = str(rec.get("name", "") or "")
    board = board_of(symbol)

    if board == "BSE" or "ST" in name.upper() or name[:1] in ("N", "C"):
        return ["ST/北交所/N/C 字头新股"], {}

    fbt = norm_fbt(rec.get("fbt"))
    threshold = SECOND_BOARD_SEAL_TIME_STAR if board == "STAR" else SECOND_BOARD_SEAL_TIME_MAIN
    if not fbt:
        reasons.append("首封时间缺失")
    elif fbt >= threshold:
        reasons.append(f"首封时间过晚：{fmt_fbt(fbt)} 晚于 {fmt_fbt(threshold)}")

    price = _to_float(rec.get("latest"))
    if price <= 0 and daily is not None and not daily.empty:
        price = _to_float(daily.sort_values("trade_date")["close"].iloc[-1])
    cap_yi = _to_float(rec.get("circular_cap")) / 1e8
    seal = _to_float(rec.get("seal_amount"))
    turnover = _to_float(rec.get("turnover"), default=float("nan"))
    gain5 = prev5_gain(daily)

    if price <= 0 or cap_yi <= 0 or turnover != turnover:  # 价格/市值/换手缺失
        reasons.append("价格/市值/换手数据缺失")
    else:
        seal_ratio = seal / (cap_yi * 1e8) * 100
        if seal <= 0:
            reasons.append("封板资金缺失")
        elif seal_ratio < SECOND_BOARD_SEAL_RATIO_MIN:
            reasons.append(f"封单力度不足：{seal_ratio:.1f}% < {SECOND_BOARD_SEAL_RATIO_MIN:.0f}%")
        if turnover >= SECOND_BOARD_TURNOVER_MAX:
            reasons.append(f"换手率过高：{turnover:.1f}% ≥ {SECOND_BOARD_TURNOVER_MAX:.0f}%")
        if not (SECOND_BOARD_CAP_MIN_YI <= cap_yi <= SECOND_BOARD_CAP_MAX_YI):
            reasons.append(f"流通市值超标：{cap_yi:.0f} 亿不在 {SECOND_BOARD_CAP_MIN_YI:.0f}-{SECOND_BOARD_CAP_MAX_YI:.0f} 亿")
        if not (SECOND_BOARD_PRICE_MIN <= price <= SECOND_BOARD_PRICE_MAX):
            reasons.append(f"股价超标：{price:.2f} 元不在 {SECOND_BOARD_PRICE_MIN:.0f}-{SECOND_BOARD_PRICE_MAX:.0f} 元")

    if gain5 is None:
        reasons.append("历史行情不足")
    elif gain5 >= SECOND_BOARD_PREV5_GAIN_MAX:
        reasons.append(f"前 5 日涨幅过高：{gain5:.1f}% ≥ {SECOND_BOARD_PREV5_GAIN_MAX:.0f}%")

    info = {"price": price, "cap_yi": cap_yi, "turnover": turnover, "prev5_gain": gain5,
            "seal_ratio": round(seal / (cap_yi * 1e8) * 100, 2) if cap_yi > 0 and seal > 0 else None,
            "fbt": fbt, "board": board}
    return reasons, info


def score_candidate(rec: dict, info: dict, sector_fbt_rank: int, broke_high: bool, on_lhb: bool) -> tuple[int, list[str]]:
    """
    软性评分（方案满分口径；研报/互动易利好与知名游资席位无离线数据，不自动评分）。
    sector_fbt_rank：板块内首封时间名次（1 起）；broke_high：突破前高平台；on_lhb：当日龙虎榜净买入。
    """
    name = str(rec.get("name", "") or "")
    score, notes = 0, []
    if sector_fbt_rank == 1:
        score += 2
        notes.append("板块首封第 1（龙一）+2")
    elif sector_fbt_rank <= 3:
        score += 1
        notes.append(f"板块首封第 {sector_fbt_rank} +1")
    if broke_high:
        score += 2
        notes.append("突破前高/平台 +2")
    if on_lhb:
        score += 1
        notes.append("龙虎榜净买入 +1（机构/游资席位人工核对）")
    if any(k in name for k in SECOND_BOARD_HOT_KEYWORDS):
        score += 1
        notes.append("名称带热点关键词 +1")
    if 0 < info.get("cap_yi", 0) < SECOND_BOARD_SMALL_CAP_YI:
        score += 1
        notes.append(f"流通市值 {info['cap_yi']:.0f} 亿 <{SECOND_BOARD_SMALL_CAP_YI:.0f} 亿 +1")
    price = info.get("price", 0)
    if price > 0 and abs(price - round(price)) < 0.005:
        score += 1
        notes.append(f"涨停价 {price:.2f} 为整数关口 +1")
    return score, notes


def screen_first_boards(
    records: list[dict],
    daily_map: dict[str, pd.DataFrame],
    lhb_net: Optional[dict[str, float]] = None,
) -> tuple[list[dict], dict[str, int], int]:
    """
    首板筛选纯函数：硬过滤 + 软评分。
    records：limit_pool 当日 pool_type=up 且 lbc≤1 的行（dict 列表）；
    daily_map：symbol → 日线 DataFrame（含首板当日）；lhb_net：symbol → 当日龙虎榜净买额。
    返回 (候选列表（评分降序，未截断）, 硬过滤原因计数, 评分不达标只数)。
    """
    lhb_net = lhb_net or {}

    # 板块内首封名次（全部首板股参与排名，龙头地位是客观事实，不受硬过滤影响）
    by_sector: dict[str, list[tuple[str, str]]] = {}
    for r in records:
        by_sector.setdefault(str(r.get("sector", "") or ""), []).append(
            (norm_fbt(r.get("fbt")) or "999999", str(r.get("symbol", "")))
        )
    fbt_rank: dict[str, int] = {}
    for sec, items in by_sector.items():
        for rank, (_, sym) in enumerate(sorted(items), start=1):
            fbt_rank[sym] = rank

    candidates: list[dict] = []
    excluded_reasons: dict[str, int] = {}
    below_score = 0
    for r in records:
        symbol = str(r.get("symbol", "")).zfill(6)[-6:]
        daily = daily_map.get(symbol)
        reasons, info = hard_filter_reasons({**r, "symbol": symbol}, daily)
        if reasons:
            for reason in reasons:
                key = reason.split("：")[0]  # 归并同类原因计数
                excluded_reasons[key] = excluded_reasons.get(key, 0) + 1
            continue
        score, notes = score_candidate(
            r, info, fbt_rank.get(symbol, 99),
            broke_high=broke_prior_high(daily),
            on_lhb=lhb_net.get(symbol, 0) > 0,
        )
        if score < SECOND_BOARD_MIN_SCORE:
            below_score += 1
            continue
        close = info["price"]
        candidates.append({
            "symbol": symbol,
            "name": str(r.get("name", "") or ""),
            "sector": str(r.get("sector", "") or ""),
            "board": _BOARD_NAMES.get(info["board"], info["board"]),
            "score": score,
            "score_notes": notes,
            "close": round(close, 2),
            "fbt": fmt_fbt(info["fbt"]),
            "seal_ratio": info.get("seal_ratio"),
            "turnover": round(info["turnover"], 2),
            "cap_yi": round(info["cap_yi"], 1),
            "prev5_gain": info.get("prev5_gain"),
            "limit_up_price": round(close * (1 + limit_up_pct(symbol)), 2),
            "stop_price": round(close * (1 - SECOND_BOARD_STOP_PCT), 2),
            "on_lhb": lhb_net.get(symbol, 0) > 0,
        })
    candidates.sort(key=lambda c: (-c["score"], c["fbt"]))
    return candidates, excluded_reasons, below_score


def _load_lhb_net(db_path: Optional[Path]) -> dict[str, float]:
    """最近一期龙虎榜净买额（symbol → 合计净买额；表为空返回 {}）"""
    try:
        from pipeline.database import get_connection

        with get_connection(db_path) as conn:
            df = pd.read_sql_query(
                "SELECT symbol, SUM(net_amount) AS net FROM dragon_tiger "
                "WHERE trade_date = (SELECT MAX(trade_date) FROM dragon_tiger) GROUP BY symbol",
                conn,
            )
        return {str(r["symbol"]).zfill(6)[-6:]: _to_float(r["net"]) for _, r in df.iterrows()}
    except Exception as e:
        logger.warning("龙虎榜读取失败（该加分项降级为 0）: %s", e)
        return {}


def build_second_board_pool(trade_date: Optional[str] = None, db_path: Optional[Path] = None) -> dict:
    """
    构建二板观察池（仅读本地 DB，离线可用）。
    trade_date 默认当日；当日涨停池为空时回退最近一期（盘前取数与扫描日期错位时兜底）。
    """
    from pipeline.database import init_database, load_daily_quotes, load_limit_pool

    init_database(db_path)
    today = trade_date or datetime.now().strftime("%Y-%m-%d")
    result = {
        "available": False, "date": today, "candidates": [],
        "total_first_boards": 0, "excluded_count": 0, "excluded_reasons": {},
        "below_score_count": 0, "note": "", "params": _params_view(),
    }

    df = load_limit_pool(trade_date=today, db_path=db_path)
    if df.empty:
        all_df = load_limit_pool(db_path=db_path)
        if not all_df.empty:
            latest = str(all_df["trade_date"].max())
            df = all_df[all_df["trade_date"] == latest]
            result["date"] = latest
    if df.empty:
        result["note"] = "涨停池为空（需先运行 run_all 取数流程）"
        return result

    up = df[df["pool_type"] == "up"]
    first = up[pd.to_numeric(up["lbc"], errors="coerce").fillna(1) <= 1]
    result["total_first_boards"] = len(first)
    if first.empty:
        result["note"] = "今日无首板股（涨停池仅含连板/炸板）"
        return result

    daily_map = {}
    for sym in first["symbol"]:
        sym = str(sym).zfill(6)[-6:]
        try:
            q = load_daily_quotes(symbol=sym, db_path=db_path)
            if not q.empty:
                daily_map[sym] = q
        except Exception as e:
            logger.debug("首板股 %s 日线读取失败（硬过滤按行情缺失处理）: %s", sym, e)

    candidates, excluded_reasons, below_score = screen_first_boards(
        first.to_dict("records"), daily_map, _load_lhb_net(db_path)
    )
    result["excluded_reasons"] = excluded_reasons
    result["excluded_count"] = int(len(first) - len(candidates) - below_score)
    result["below_score_count"] = below_score
    result["candidates"] = candidates[:SECOND_BOARD_TOP_N]
    result["available"] = True
    if not candidates:
        result["note"] = (
            f"今日 {len(first)} 只首板股无一进池：硬过滤淘汰 {result['excluded_count']} 只，"
            f"评分不足 {SECOND_BOARD_MIN_SCORE} 分 {below_score} 只"
        )
    return result


def _params_view() -> dict:
    """阈值参数透传（Web 控制台清单展示口径与 config 保持一致）"""
    return {
        "seal_time_main": fmt_fbt(SECOND_BOARD_SEAL_TIME_MAIN),
        "seal_time_star": fmt_fbt(SECOND_BOARD_SEAL_TIME_STAR),
        "seal_ratio_min": SECOND_BOARD_SEAL_RATIO_MIN,
        "turnover_max": SECOND_BOARD_TURNOVER_MAX,
        "cap_min_yi": SECOND_BOARD_CAP_MIN_YI,
        "cap_max_yi": SECOND_BOARD_CAP_MAX_YI,
        "price_min": SECOND_BOARD_PRICE_MIN,
        "price_max": SECOND_BOARD_PRICE_MAX,
        "prev5_gain_max": SECOND_BOARD_PREV5_GAIN_MAX,
        "min_score": SECOND_BOARD_MIN_SCORE,
        "stop_pct": SECOND_BOARD_STOP_PCT * 100,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    pool = build_second_board_pool()
    if not pool["available"]:
        print(pool["note"] or "二板观察池不可用")
        return
    print(f"=== 二板观察池（首板日 {pool['date']}，首板 {pool['total_first_boards']} 只 → "
          f"硬过滤淘汰 {pool['excluded_count']} / 评分不足 {pool['below_score_count']} → "
          f"入池 {len(pool['candidates'])} 只）===")
    for c in pool["candidates"]:
        print(f"{c['symbol']} {c['name']}（{c['board']}·{c['sector']}）评分 {c['score']} | "
              f"首封 {c['fbt']} 封单 {c['seal_ratio']}% 换手 {c['turnover']}% "
              f"市值 {c['cap_yi']} 亿 现价 {c['close']} 前5日 {c['prev5_gain']}% | "
              f"次日涨停 {c['limit_up_price']} 硬止损 {c['stop_price']} | {'；'.join(c['score_notes'])}")
    if pool["note"]:
        print(pool["note"])


if __name__ == "__main__":
    main()
