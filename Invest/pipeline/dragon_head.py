"""
龙头识别评分 — 《如何识别真假龙头》三维验证的可量化落地（超短 HOT-S 候选用）

五维评分（满分 100，全部离线数据，无未来函数）：
- 身位(30)：板块内连板最高（lbc≥2）→ 30；首板且首次封板时间板块内前 3 → 20；跟风 → 8
- 梯队(20)：板块涨停 ≥5 家且连板层级 ≥2 → 20；3-4 家 → 12；1-2 家 → 4（独木难支）
- 强度(20)：一字板 → 4（无量，要么买不到）；否则换手率 15-35% → 10、5-15/35-50% → 6、其他 3；
  封板资金 ≥1 亿 → 10、≥5000 万 → 6、<1000 万 → 2、其他 3；炸板次数 ≥3 → 强度分减半
- 逻辑(20)：⑧催化 satisfied True → 20；None → 8（待人工核对）；False → 0
- 情绪(10)：当日涨停 ≥50 家 → 10；30-49 → 6；20-29 → 3；<20 → 0

等级段与 V5.0 凸性评分一致：S ≥ 80 / A 65-79 / B 50-64 / C < 50。
量化只输出身位/强度/梯队/情绪数据与逻辑初判，最终辨识由人完成（文档 §五边界）。

用法:
    python pipeline/dragon_head.py            # 打印今日龙头评分榜（离线读 market.db）
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
from typing import Optional

import pandas as pd

from pipeline.database import get_connection, init_database

logger = logging.getLogger(__name__)

GRADE_S, GRADE_A, GRADE_B = 80, 65, 50  # 等级分数线（V5.0 凸性评分段口径）


def build_dragon_context(trade_date: str, db_path: Optional[Path] = None) -> dict:
    """
    构建龙头评分上下文（离线）：
    - sectors: {板块名: {count, max_lbc, levels, leaders(最高板symbol集), first_fbts(封板时间升序列表)}}
    - emotion: {limit_up_count, up_count, down_count}
    """
    init_database(db_path)
    sectors: dict[str, dict] = {}
    emotion = {"limit_up_count": 0, "up_count": 0, "down_count": 0}

    with get_connection(db_path) as conn:
        try:
            df = pd.read_sql_query(
                "SELECT symbol, name, sector, lbc, fbt FROM limit_pool "
                "WHERE trade_date = ? AND pool_type = 'up'",
                conn, params=(trade_date,),
            )
        except Exception as e:
            logger.warning("涨停池读取失败（梯队为空）: %s", e)
            df = pd.DataFrame()
        try:
            row = conn.execute(
                "SELECT limit_up_count, up_count, down_count FROM limit_stats "
                "ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
        except Exception as e:
            logger.warning("市场宽度读取失败（情绪为 0）: %s", e)
            row = None

    if row:
        emotion = {"limit_up_count": int(row[0] or 0),
                   "up_count": int(row[1] or 0), "down_count": int(row[2] or 0)}

    for _, r in df.iterrows():
        sec = str(r.get("sector", "") or "")
        if not sec:
            continue
        s = sectors.setdefault(sec, {"count": 0, "max_lbc": 0, "lbcs": [], "leaders": [], "fbts": []})
        s["count"] += 1
        lbc = int(r.get("lbc", 0) or 0)
        s["lbcs"].append(lbc)
        if lbc > s["max_lbc"]:
            s["max_lbc"] = lbc
            s["leaders"] = [str(r["symbol"])]
        elif lbc == s["max_lbc"] and lbc > 0:
            s["leaders"].append(str(r["symbol"]))
        fbt = str(r.get("fbt", "") or "")
        if fbt:
            s["fbts"].append(fbt)

    for s in sectors.values():
        s["levels"] = len({x for x in s["lbcs"] if x > 0})
        s["fbts"] = sorted(s["fbts"])
    return {"trade_date": trade_date, "sectors": sectors, "emotion": emotion}


def _score_position(rec: dict, sec_ctx: dict) -> tuple[int, str]:
    """身位（30）：板块内连板最高且 lbc≥2 → 30；首板且首次封板时间板块前 3 → 20；跟风 → 8"""
    lbc = int(rec.get("lbc", 0) or 0)
    symbol = str(rec.get("symbol", ""))
    if not sec_ctx:
        return 8, "无板块梯队数据，按跟风计"
    max_lbc = sec_ctx.get("max_lbc", 0)
    if lbc >= 2 and lbc >= max_lbc and symbol in sec_ctx.get("leaders", []):
        return 30, f"板块最高板（{lbc} 连板）"
    if lbc <= 1:
        fbt = str(rec.get("fbt", "") or "")
        fbts = sec_ctx.get("fbts", [])
        if fbt and fbt in fbts[:3]:
            return 20, f"首板且板块内封板时间前 3（{fbt}）"
    return 8, f"跟风（lbc={lbc}，板块最高 {max_lbc} 板）"


def _score_echelon(sec_ctx: dict) -> tuple[int, str]:
    """梯队（20）：涨停 ≥5 家且连板层级 ≥2 → 20；3-4 家 → 12；1-2 家 → 4"""
    if not sec_ctx:
        return 4, "无板块梯队数据"
    count, levels = sec_ctx.get("count", 0), sec_ctx.get("levels", 0)
    if count >= 5 and levels >= 2:
        return 20, f"板块涨停 {count} 家、{levels} 级梯队"
    if count >= 3:
        return 12, f"板块涨停 {count} 家"
    return 4, f"板块仅 {count} 家涨停，独木难支"


def _score_strength(rec: dict) -> tuple[int, str]:
    """强度（20）：一字板 → 4；换手率 15-35% → 10、5-15/35-50% → 6、其他 3；
    封板资金 ≥1 亿 → 10、≥5000 万 → 6、<1000 万 → 2、其他 3；炸板 ≥3 次 → 总分减半"""
    if rec.get("one_word_board"):
        return 4, "一字板（无量，要么买不到要么坑）"
    turnover = float(rec.get("turnover", 0) or 0)
    if 15 <= turnover <= 35:
        t_score = 10
    elif 5 <= turnover < 15 or 35 < turnover <= 50:
        t_score = 6
    else:
        t_score = 3
    seal = float(rec.get("seal_amount", 0) or 0)
    if seal >= 1e8:
        s_score = 10
    elif seal >= 5e7:
        s_score = 6
    elif seal < 1e7:
        s_score = 2
    else:
        s_score = 3
    total = t_score + s_score
    note = f"换手 {turnover:.1f}%（{t_score}）+ 封单 {seal / 1e8:.2f} 亿（{s_score}）"
    zbc = int(rec.get("zbc", 0) or 0)
    if zbc >= 3:
        total //= 2
        note += f"；炸板 {zbc} 次减半"
    return total, note


def _score_logic(rec: dict) -> tuple[int, str]:
    """逻辑（20）：⑧催化 satisfied True → 20；None → 8（人工核对）；False → 0"""
    cat = rec.get("catalyst") or {}
    satisfied = cat.get("satisfied")
    if satisfied is True:
        ctype = cat.get("catalyst_type", "")
        return 20, f"事件催化确认（{ctype}）" if ctype else "事件催化确认"
    if satisfied is False:
        return 0, "无明确催化"
    return 8, "催化待人工核对"


def _score_emotion(ctx: dict) -> tuple[int, str]:
    """情绪（10）：涨停 ≥50 → 10；30-49 → 6；20-29 → 3；<20 → 0"""
    n = (ctx.get("emotion") or {}).get("limit_up_count", 0)
    if n >= 50:
        return 10, f"涨停 {n} 家，情绪热"
    if n >= 30:
        return 6, f"涨停 {n} 家"
    if n >= 20:
        return 3, f"涨停 {n} 家，情绪偏弱"
    return 0, f"涨停 {n} 家，冰点，独龙难活"


def score_dragon(rec: dict, ctx: dict) -> dict:
    """
    对单只候选打龙头评分。rec 需含 symbol/lbc/sector/fbt/turnover/seal_amount/zbc，
    可选 one_word_board（一字板标记）、catalyst（⑧判定 dict）。
    """
    sec_ctx = (ctx.get("sectors") or {}).get(str(rec.get("sector", "") or ""), {})
    dims: dict[str, int] = {}
    notes: list[str] = []

    dims["身位"], n1 = _score_position(rec, sec_ctx)
    dims["梯队"], n2 = _score_echelon(sec_ctx)
    dims["强度"], n3 = _score_strength(rec)
    dims["逻辑"], n4 = _score_logic(rec)
    dims["情绪"], n5 = _score_emotion(ctx)
    notes = [n1, n2, n3, n4, n5]

    score = sum(dims.values())
    grade = "S" if score >= GRADE_S else "A" if score >= GRADE_A else "B" if score >= GRADE_B else "C"
    return {"score": score, "grade": grade, "dims": dims, "notes": notes}


def grade_hot_candidates(records: list[dict], ctx: dict) -> list[dict]:
    """对热点候选列表逐条打龙头分（原地附加 dragon_score/dragon_grade/dragon_dims/dragon_notes）"""
    for rec in records:
        try:
            r = score_dragon(rec, ctx)
            rec["dragon_score"] = r["score"]
            rec["dragon_grade"] = r["grade"]
            rec["dragon_dims"] = r["dims"]
            rec["dragon_notes"] = r["notes"]
        except Exception as e:
            logger.warning("龙头评分失败 %s（跳过）: %s", rec.get("symbol"), e)
    return records


def dragon_top(records: list[dict], grades: tuple = ("S", "A", "B")) -> list[dict]:
    """取等级达标子集，按分数降序"""
    return sorted(
        [r for r in records if r.get("dragon_grade") in grades],
        key=lambda r: -r.get("dragon_score", 0),
    )


def main():
    """打印今日涨停池龙头评分榜（离线）"""
    import argparse
    from datetime import datetime

    parser = argparse.ArgumentParser(description="龙头评分榜（离线读 market.db）")
    parser.add_argument("--date", default=None, help="交易日 YYYY-MM-DD（默认 limit_pool 最新日期）")
    args = parser.parse_args()

    init_database()
    date = args.date
    if not date:
        with get_connection() as conn:
            row = conn.execute("SELECT MAX(trade_date) FROM limit_pool").fetchone()
        date = row[0] if row and row[0] else None
    if not date:
        print("limit_pool 为空，请先运行盘前流程")
        return

    ctx = build_dragon_context(date)
    with get_connection() as conn:
        df = pd.read_sql_query(
            "SELECT * FROM limit_pool WHERE trade_date = ? AND pool_type = 'up' ORDER BY lbc DESC",
            conn, params=(date,),
        )
    records = df.to_dict("records")
    grade_hot_candidates(records, ctx)
    top = dragon_top(records, grades=("S", "A", "B", "C"))
    print(f"\n=== 龙头评分榜 {date}（涨停 {ctx['emotion']['limit_up_count']} 家）===\n")
    print(f"{'等级':<4}{'分数':<6}{'代码':<8}{'名称':<10}{'板块':<8}{'连板':<4}五维（身位/梯队/强度/逻辑/情绪）")
    for r in top[:30]:
        d = r["dragon_dims"]
        print(f"{r['dragon_grade']:<4}{r['dragon_score']:<6}{r['symbol']:<8}"
              f"{str(r.get('name','')):<10}{str(r.get('sector','')):<8}{r.get('lbc',0):<4}"
              f"{d['身位']}/{d['梯队']}/{d['强度']}/{d['逻辑']}/{d['情绪']}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
