"""
个股证据链深挖测试 — mock 联网 LLM 与缓存，不触网
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import research.evidence_chain as ec
from research.event_calendar import _parse_sector_events


VALID_JSON = json.dumps({
    "ignition": "金华国资入主获批复",
    "chain": "国资入主 → 授信与订单资源注入 → CRO 产能利用率提升 → 估值修复",
    "positives": ["国资入主获批（公告 8-05）", "中报预增 40%（业绩预告）"],
    "negatives": ["CRO 板块整体退潮", "换手不足一字板"],
    "relations": [{"symbol_name": "药明康德", "relation": "同概念龙头", "note": "CRO 中军"}],
    "industry_position": "区域 CRO 二线，金华国资控股",
}, ensure_ascii=False)


class TestParseEvidence:
    def test_valid_json(self):
        ev = ec._parse_evidence(VALID_JSON)
        assert ev["ignition"] == "金华国资入主获批复"
        assert len(ev["positives"]) == 2
        assert ev["relations"][0]["relation"] == "同概念龙头"

    def test_garbage_returns_none(self):
        assert ec._parse_evidence("不是 JSON") is None
        assert ec._parse_evidence(None) is None
        assert ec._parse_evidence('{"foo": 1}') is None  # 无 ignition 字段

    def test_lists_truncated(self):
        big = json.dumps({
            "ignition": "x", "positives": [f"p{i}" for i in range(10)],
            "negatives": ["n"], "relations": [], "chain": "", "industry_position": "",
        })
        ev = ec._parse_evidence(big)
        assert len(ev["positives"]) == 4


class TestDigEvidence:
    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(ec, "KIMI_API_KEY", "")
        assert ec.dig_evidence("600721", "百花医药", "医疗服务", "CRO+创新药") is None

    def test_llm_success_and_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ec, "KIMI_API_KEY", "sk-test")
        monkeypatch.setattr(ec, "EVIDENCE_CACHE_DIR", tmp_path)
        calls = []
        monkeypatch.setattr(ec, "_call_kimi_with_web_search",
                            lambda *a, **kw: calls.append(1) or VALID_JSON)

        ev1 = ec.dig_evidence("600721", "百花医药", "医疗服务", "CRO+创新药",
                              trade_date="2026-08-07")
        assert ev1["ignition"]
        assert len(calls) == 1
        # 第二次命中缓存，不再调用
        ev2 = ec.dig_evidence("600721", "百花医药", "医疗服务", "CRO+创新药",
                              trade_date="2026-08-07")
        assert ev2 == ev1
        assert len(calls) == 1

    def test_llm_failure_degrades(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ec, "KIMI_API_KEY", "sk-test")
        monkeypatch.setattr(ec, "EVIDENCE_CACHE_DIR", tmp_path)  # 隔离缓存，避免真实缓存命中
        monkeypatch.setattr(ec, "_call_kimi_with_web_search",
                            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("reset")))
        assert ec.dig_evidence("600721", "百花医药", "医疗服务", "x") is None

    def test_build_chains_top_n(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ec, "KIMI_API_KEY", "sk-test")
        monkeypatch.setattr(ec, "EVIDENCE_CACHE_DIR", tmp_path)
        monkeypatch.setattr(ec, "_call_kimi_with_web_search", lambda *a, **kw: VALID_JSON)
        cands = [
            {"symbol": "600721", "name": "百花医药", "sector": "医疗服务", "reason": "CRO"},
            {"symbol": "300615", "name": "欣天科技", "sector": "通信设备", "reason": "5G"},
            {"symbol": "600272", "name": "开开实业", "sector": "医药商业", "reason": "SPD"},
            {"symbol": "603459", "name": "红板科技", "sector": "元件", "reason": "PCB"},
        ]
        chains = ec.build_evidence_chains(cands, trade_date="2026-08-07", max_n=2)
        assert set(chains) == {"600721", "300615"}  # 只深挖前 2 只


class TestSectorEventDedup:
    def test_identical_events_merged(self):
        text = json.dumps({"events": [
            {"sector": "化学制药", "date": "8月", "type": "业绩", "title": "半年报集中披露期", "chain": "a", "source": "x"},
            {"sector": "通信设备", "date": "8月", "type": "业绩", "title": "半年报集中披露期", "chain": "b", "source": "x"},
            {"sector": "化学制药", "date": "8-20", "type": "会议", "title": "医药大会", "chain": "c", "source": "y"},
        ]}, ensure_ascii=False)
        out = _parse_sector_events(text, {"化学制药", "通信设备"})
        assert len(out) == 2
        dup = [e for e in out if e["title"] == "半年报集中披露期"][0]
        assert dup["sector"] == "多板块"


class TestPayloadEvidenceInjection:
    def test_evidence_in_candidate_lines(self):
        from research.dragon_reasoner import build_reasoning_payload

        c = {
            "symbol": "600721", "name": "百花医药", "sector": "医疗服务", "lbc": 4,
            "turnover": 15.6, "seal_amount": 1.6e8, "fbt": "093100", "zbc": 0,
            "dragon_score": 80, "dragon_dims": {"身位": 30, "梯队": 20, "强度": 18, "逻辑": 12, "情绪": 10},
            "dragon_notes": [], "catalyst": None, "reason": "CRO+创新药",
        }
        evidence = {"600721": {
            "ignition": "金华国资入主获批", "industry_position": "区域 CRO 二线",
            "positives": ["国资入主获批", "中报预增 40%"], "negatives": ["CRO 板块退潮"],
            "relations": [], "chain": "",
        }}
        p = build_reasoning_payload([c], {"sectors": {}, "emotion": {}}, "C", evidence=evidence)
        assert "引爆点：金华国资入主获批" in p["candidate_lines"]
        assert "行业地位：区域 CRO 二线" in p["candidate_lines"]
        assert "正面证据" in p["candidate_lines"]
