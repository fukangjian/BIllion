"""
候选催化分析 — 买入规则⑧（事件/政策/业绩/技术突破/转型）自动判定

数据链：公告列表（announcement_fetcher，东财/巨潮 fallback）→ 关键词筛出催化相关公告
→ 正文获取（缓存 aside）→ has_real_content 护栏 → LLM 结构化判定（24h 缓存）。
每一级失败都降级：无 LLM Key / 无正文 / 网络失败时返回 satisfied=None（人工核对），
但始终返回近期公告标题（事实信息，不编造）。
"""
import logging
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from config import CATALYST_ANNOUNCE_DAYS

logger = logging.getLogger(__name__)

# 催化相关公告标题关键词（业绩/事件/政策/技术突破/转型）
_CATALYST_KEYWORDS = (
    "预增", "扭亏", "业绩", "利润", "中标", "合同", "订单", "收购", "重组",
    "资产", "转型", "合作", "协议", "战略", "研发", "突破", "专利", "获批",
    "批准", "上市", "投产", "产能", "回购", "增持", "股权激励", "分红", "政府补助",
)

# 判定结果解析用
_VERDICT_RE = re.compile(r"判定[:：]\s*(满足|不满足)")
_TYPE_RE = re.compile(r"催化类型[:：]\s*(\S+)")
_SUSTAIN_RE = re.compile(r"持续性[:：]\s*(.+)")
_DATE_RE = re.compile(r"事件日期[:：]\s*(\d{4}-\d{2}-\d{2}|未知)")
_STRENGTH_RE = re.compile(r"事件力度[:：]\s*(重磅|中等|轻微)")
_WAVE_RE = re.compile(r"炒作波次[:：]\s*(首次|第二波及以上|无法判断)")
_BASIS_RE = re.compile(r"依据[:：]\s*(.+)")


def _pick_relevant_announcements(df: pd.DataFrame, limit: int = 3) -> pd.DataFrame:
    """从公告列表筛出催化相关（标题关键词命中优先），不足则用最新公告补齐"""
    if df is None or df.empty:
        return pd.DataFrame()
    hit = df[df["title"].astype(str).apply(lambda t: any(k in t for k in _CATALYST_KEYWORDS))]
    picked = hit.head(limit)
    if len(picked) < limit:
        rest = df[~df.index.isin(picked.index)].head(limit - len(picked))
        picked = pd.concat([picked, rest])
    return picked


def parse_verdict(text: str) -> dict | None:
    """解析 LLM 七行格式输出（事件日期/力度/波次为 2026-09 扩展字段，缺失时为空，向后兼容）；
    无法解析返回 None"""
    if not text:
        return None
    m = _VERDICT_RE.search(text)
    if not m:
        return None

    def _field(rx: re.Pattern) -> str:
        f = rx.search(text)
        return f.group(1).strip() if f else ""

    return {
        "satisfied": m.group(1) == "满足",
        "catalyst_type": _field(_TYPE_RE),
        "sustainability": _field(_SUSTAIN_RE),
        "event_date": _field(_DATE_RE),
        "strength": _field(_STRENGTH_RE),
        "wave": _field(_WAVE_RE),
        "basis": _field(_BASIS_RE),
    }


def event_factor(catalyst: dict | None, today: str = "") -> dict:
    """
    催化判定 → 事件驱动因子（EVT-S 排序用，纯函数）。

    评分（满分 100，入选线 config.EVT_MIN_EVENT_SCORE=70）：
    - 基础 20：⑧判定 satisfied=True 才有（不满足/无法判定 → 总分 0，事件驱动不参与）
    - 力度 0-40：重磅 40 / 中等 24 / 轻微 10 / 未标注（旧数据回退）20
    - 时效 0-25：事件日期距 today ≤1 日 25 / ≤3 日 18 / ≤5 日 8 / 更早或未知 2
    - 波次 0-12：首次 12 / 第二波及以上 3 / 无法判断（回退）6

    校准口径：重磅任意新鲜度均入选；中等须 ≤3 日内且首次/第二波新鲜；轻微一律不入选。

    返回 {"score": int, "breakdown": str, "days_since": int|None}；
    catalyst 缺失或未判定 → score 0（不构成事件驱动候选，但保留原 ⑧ 口径不受影响）。
    """
    cat = catalyst or {}
    if cat.get("satisfied") is not True:
        return {"score": 0, "breakdown": "无确认催化", "days_since": None}

    strength = str(cat.get("strength", "") or "")
    strength_score = {"重磅": 40, "中等": 24, "轻微": 10}.get(strength, 20)

    days_since = None
    freshness = 2
    event_date = str(cat.get("event_date", "") or "")
    if event_date and today:
        try:
            from datetime import datetime

            d = (datetime.strptime(today, "%Y-%m-%d")
                 - datetime.strptime(event_date, "%Y-%m-%d")).days
            if d >= 0:
                days_since = d
                freshness = 25 if d <= 1 else 18 if d <= 3 else 8 if d <= 5 else 2
        except ValueError:
            pass

    wave = str(cat.get("wave", "") or "")
    wave_score = {"首次": 12, "第二波及以上": 3}.get(wave, 6)

    score = min(100, 20 + strength_score + freshness + wave_score)
    parts = [f"基础20+力度{strength_score}（{strength or '未标注'}）",
             f"时效{freshness}（{f'{days_since}日前' if days_since is not None else '日期未知'}）",
             f"波次{wave_score}（{wave or '未知'}）"]
    return {"score": score, "breakdown": "；".join(parts), "days_since": days_since}


def analyze_catalyst(symbol: str, name: str = "", sector: str = "") -> dict:
    """
    判定单只股票买入规则⑧是否满足。

    返回 {"satisfied": bool|None, "catalyst_type": str, "sustainability": str,
          "basis": str, "titles": [str]}
    satisfied=None 表示无法自动判定（降级为人工核对），titles 始终尽量给出（事实）。
    """
    symbol = str(symbol).zfill(6)[-6:]
    result = {"satisfied": None, "catalyst_type": "", "sustainability": "",
              "event_date": "", "strength": "", "wave": "", "basis": "", "titles": []}

    # 1) 近期公告列表（东财/巨潮 fallback，网络失败降级空表）
    try:
        from research.announcement_fetcher import fetch_latest_announcements

        df = fetch_latest_announcements(symbol, limit=8, days=CATALYST_ANNOUNCE_DAYS)
    except Exception as e:
        logger.warning("催化分析：%s 公告列表获取失败（降级人工核对）: %s", symbol, e)
        result["basis"] = "公告获取失败，需人工核对"
        return result

    if df is None or df.empty:
        result["basis"] = f"近 {CATALYST_ANNOUNCE_DAYS} 日无公告数据，需人工核对"
        return result

    result["titles"] = [
        f"{r.get('date', '')}《{r.get('title', '')}》" for _, r in df.head(3).iterrows()
    ]
    picked = _pick_relevant_announcements(df, limit=3)

    # 2) 无 LLM Key：只给公告标题事实，不判定（防编造）
    from shared.llm_client import call_llm, has_llm_api_key

    if not has_llm_api_key():
        result["basis"] = "；".join(result["titles"][:2]) + "（未配置 LLM，需人工核对）"
        return result

    # 3) 正文获取 + has_real_content 护栏（无正文禁止调用 LLM）
    from research.announcement_fetcher import fetch_announcement_full_text, has_real_content

    contents = []
    for _, r in picked.head(2).iterrows():
        try:
            text = fetch_announcement_full_text(
                str(r.get("url", "")), symbol=symbol,
                title=str(r.get("title", "")), date=str(r.get("date", "")),
            )
        except Exception as e:
            logger.warning("催化分析：%s 公告正文获取失败（跳过该篇）: %s", symbol, e)
            continue
        if has_real_content(text):
            contents.append(f"《{r.get('title', '')}》({r.get('date', '')})\n{text[:4000]}")

    titles_text = "\n".join(f"- {t}" for t in result["titles"])
    if not contents:
        result["basis"] = "；".join(result["titles"][:2]) + "（未取得公告正文，需人工核对）"
        return result

    # 4) LLM 结构化判定（24h 缓存，失败降级）
    from shared.prompts import CATALYST_ANALYSIS, CATALYST_SYSTEM

    prompt = CATALYST_ANALYSIS.format(
        symbol=symbol,
        stock_name=name or symbol,
        sector=sector or "未知",
        titles=titles_text,
        content="\n\n---\n\n".join(contents)[:8000],
    )
    response = call_llm(prompt, system_prompt=CATALYST_SYSTEM, task_type="announcement")
    verdict = parse_verdict(response or "")
    if verdict is None:
        logger.warning("催化分析：%s LLM 输出无法解析（降级人工核对）", symbol)
        result["basis"] = "；".join(result["titles"][:2]) + "（LLM 判定失败，需人工核对）"
        return result

    result.update(verdict)
    return result


def main():
    """CLI：python research/catalyst_analyzer.py 600162 [--name 香江控股] [--sector 房地产开发]"""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="候选催化分析（买入规则⑧）")
    parser.add_argument("symbol", help="股票代码")
    parser.add_argument("--name", default="", help="股票名称")
    parser.add_argument("--sector", default="", help="所属板块")
    args = parser.parse_args()

    res = analyze_catalyst(args.symbol, name=args.name, sector=args.sector)
    verdict = {True: "满足", False: "不满足", None: "无法判定（人工核对）"}[res["satisfied"]]
    print(f"\n=== 催化分析 {args.symbol} {args.name} ===")
    print(f"判定: {verdict}")
    if res["catalyst_type"]:
        print(f"催化类型: {res['catalyst_type']}")
    if res["sustainability"]:
        print(f"持续性: {res['sustainability']}")
    if res["basis"]:
        print(f"依据: {res['basis']}")
    if res["titles"]:
        print("近期公告:")
        for t in res["titles"]:
            print(f"  - {t}")


if __name__ == "__main__":
    main()
