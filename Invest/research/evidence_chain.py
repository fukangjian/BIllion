"""
个股证据链深挖 — 引爆点 / 链式推导 / 正反证据 / 关联个股（K2.6 联网检索）

针对「个股核心证据雷同、只有财报预约日期」的缺口：对量化评分 Top N 的龙头候选，
逐股调用 Kimi K2.6 + $web_search 检索近 1-3 个月公司公告/新闻/行业动态，
产出结构化证据链（引爆点、链式推导、正/反证据、关联个股、行业地位），
注入辨龙头推理（dragon_reasoner payload）并在报告 / 计划 / UI 展示。

成本口径：每股每日最多 1 次联网调用，data/evidence_chain/ 当日缓存复用；
无 Key / 调用失败 / 解析失败降级 None（调用方不显示，不阻塞主流程）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import logging
from datetime import datetime
from typing import Optional

from config import DATA_DIR, DRAGON_EVIDENCE_MAX, KIMI_API_KEY
from research.event_calendar import _call_kimi_with_web_search

logger = logging.getLogger(__name__)

EVIDENCE_CACHE_DIR = DATA_DIR / "evidence_chain"

_SYSTEM = """你是 A 股短线题材研究助理。利用联网搜索，深挖指定个股近 1-3 个月的真实动态。
只输出 JSON；所有证据须来自搜索到的公开信息；搜不到就明说，不得编造。"""

_PROMPT = """今天是 {date}。请联网检索 A 股公司 {name}（{symbol}，{sector}板块）近 1-3 个月的公告、新闻与行业动态，
并结合其涨停归因「{reason}」，回答：这只股票这波行情真正的引爆点是什么？

输出严格 JSON（不要 markdown 代码块）：
{{
  "ignition": "引爆点一句话（具体事件：如 XX 国资入主获批 / XX 大单品中标 / XX 产品价格单月涨 30%；禁止'涨停''业绩好'这类空话）",
  "chain": "链式推导 3-4 步：事件 → 对收入/利润的传导 → 市场预期差 → 股价反应",
  "positives": ["正面证据1（含来源与数据）", "正面证据2", "正面证据3"],
  "negatives": ["反面证据1（质疑/证伪点/风险）", "反面证据2"],
  "relations": [{{"symbol_name": "关联个股名", "relation": "同概念龙头/跟风/上下游/补涨", "note": "一句话"}}],
  "industry_position": "行业地位一句话（细分排名/市占率/角色）"
}}

要求：正/反证据各 2-4 条；关联个股不超过 3 个；检索不到足够信息时对应字段写「未检索到」，不要编造。
**最多进行 2 次搜索，之后必须停止检索并输出 JSON 结论**。"""


def _parse_evidence(text: str | None) -> Optional[dict]:
    """解析证据链 JSON（护栏：字段截断、列表 ≤4、解析失败 None）"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`").lstrip("jsonJSON").strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("ignition"):
        return None
    relations = [
        {
            "symbol_name": str(r.get("symbol_name", ""))[:20],
            "relation": str(r.get("relation", ""))[:20],
            "note": str(r.get("note", ""))[:80],
        }
        for r in (data.get("relations") or [])[:3]
        if isinstance(r, dict)
    ]
    return {
        "ignition": str(data.get("ignition", ""))[:200],
        "chain": str(data.get("chain", ""))[:400],
        "positives": [str(p)[:150] for p in (data.get("positives") or [])[:4]],
        "negatives": [str(n)[:150] for n in (data.get("negatives") or [])[:4]],
        "relations": relations,
        "industry_position": str(data.get("industry_position", ""))[:120],
    }


def dig_evidence(
    symbol: str,
    name: str,
    sector: str,
    reason: str,
    trade_date: Optional[str] = None,
    use_cache: bool = True,
) -> Optional[dict]:
    """单股证据链深挖（K2.6 + $web_search，当日缓存）；失败/无 Key 返回 None"""
    if not KIMI_API_KEY:
        return None
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    symbol = str(symbol).zfill(6)[-6:]
    EVIDENCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = EVIDENCE_CACHE_DIR / f"{trade_date}_{symbol}.json"
    if use_cache and cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    prompt = _PROMPT.format(
        date=trade_date, name=name or symbol, symbol=symbol,
        sector=sector or "未知", reason=reason or "未知",
    )
    try:
        text = _call_kimi_with_web_search(prompt, _SYSTEM, max_rounds=5)
    except Exception as e:
        logger.warning("证据链深挖失败 %s（跳过）: %s", symbol, e)
        return None
    evidence = _parse_evidence(text)
    if evidence:
        cache_file.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


def build_evidence_chains(
    candidates: list[dict],
    trade_date: Optional[str] = None,
    max_n: int = DRAGON_EVIDENCE_MAX,
) -> dict[str, dict]:
    """
    对量化分 Top N 候选逐股深挖（顺序执行，单股失败不阻塞）。
    candidates 需含 symbol/name/sector/reason；返回 {symbol: evidence}。
    """
    chains: dict[str, dict] = {}
    for rec in candidates[:max_n]:
        symbol = str(rec.get("symbol", "")).zfill(6)[-6:]
        if not symbol:
            continue
        evidence = dig_evidence(
            symbol,
            name=str(rec.get("name", "") or ""),
            sector=str(rec.get("sector", "") or ""),
            reason=str(rec.get("reason", "") or ""),
            trade_date=trade_date,
        )
        if evidence:
            chains[symbol] = evidence
    return chains
