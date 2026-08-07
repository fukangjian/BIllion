"""
龙头深度推理测试 — mock LLM，不依赖网络、不触真实 API Key
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import research.dragon_reasoner as dr
from research.dragon_reasoner import (
    build_reasoning_payload,
    reason_dragons,
    _parse_verdicts,
)


def _cand(symbol="601700", score=85, verdict_extra=None):
    c = {
        "symbol": symbol, "name": "风范股份", "sector": "电网设备", "lbc": 4,
        "turnover": 20.0, "seal_amount": 1.2e8, "fbt": "093000", "zbc": 0,
        "dragon_score": score,
        "dragon_dims": {"身位": 30, "梯队": 20, "强度": 15, "逻辑": 20, "情绪": 10},
        "dragon_notes": ["板块最高板（4 连板）", "板块涨停 6 家、2 级梯队"],
        "catalyst": {"satisfied": True, "catalyst_type": "业绩/事件", "basis": "业绩预增公告"},
        "catalyst_titles": ["2026年半年度业绩预增公告"],
    }
    if verdict_extra:
        c.update(verdict_extra)
    return c


def _ctx():
    return {
        "sectors": {"电网设备": {"count": 6, "max_lbc": 4, "levels": 2, "leaders": ["601700"], "lbcs": [4, 1, 1], "fbts": []}},
        "emotion": {"limit_up_count": 79, "up_count": 3000, "down_count": 2000},
    }


class TestPayload:
    def test_contains_key_facts(self):
        p = build_reasoning_payload([_cand()], _ctx(), "B")
        assert p["market_state"] == "B"
        assert p["limit_up_count"] == 79
        assert "电网设备" in p["sector_lines"] and "6 家" in p["sector_lines"]
        assert "601700" in p["candidate_lines"]
        assert "4 连板" in p["candidate_lines"]
        assert "业绩预增公告" in p["candidate_lines"]


class TestParseVerdicts:
    VALID = {"601700", "002649"}

    def test_valid_json(self):
        text = '{"primary": "601700", "verdicts": [{"symbol": "601700", "verdict": "真龙头", "confidence": 85, "reasoning": "身位最高", "risk": "高位分歧"}], "market_comment": "情绪可"}'
        r = _parse_verdicts(text, self.VALID)
        assert r["primary"] == "601700"
        assert r["verdicts"][0]["verdict"] == "真龙头"
        assert r["verdicts"][0]["confidence"] == 85

    def test_markdown_wrapped(self):
        text = '```json\n{"primary": "002649", "verdicts": []}\n```'
        r = _parse_verdicts(text, self.VALID)
        assert r["primary"] == "002649"

    def test_garbage_returns_none(self):
        assert _parse_verdicts("这不是 JSON", self.VALID) is None
        assert _parse_verdicts(None, self.VALID) is None

    def test_unknown_symbol_and_bad_verdict_dropped(self):
        text = '{"verdicts": [{"symbol": "999999", "verdict": "真龙头", "confidence": 90, "reasoning": "x"}, {"symbol": "601700", "verdict": "神龙", "confidence": 90, "reasoning": "x"}, {"symbol": "601700", "verdict": "跟风", "confidence": 150, "reasoning": "y"}]}'
        r = _parse_verdicts(text, self.VALID)
        assert len(r["verdicts"]) == 1
        assert r["verdicts"][0]["verdict"] == "跟风"
        assert r["verdicts"][0]["confidence"] == 100  # 截断上限

    def test_primary_not_in_candidates_cleared(self):
        text = '{"primary": "999999", "verdicts": []}'
        assert _parse_verdicts(text, self.VALID)["primary"] == ""


class TestReasonDragons:
    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(dr, "has_llm_api_key", lambda: False)
        assert reason_dragons([_cand()], _ctx()) is None

    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setattr(dr, "DRAGON_REASON_ENABLED", False)
        assert reason_dragons([_cand()], _ctx()) is None

    def test_llm_success(self, monkeypatch):
        monkeypatch.setattr(dr, "has_llm_api_key", lambda: True)
        monkeypatch.setattr(dr, "call_llm", lambda *a, **kw: (
            '{"primary": "601700", "verdicts": [{"symbol": "601700", "verdict": "真龙头", '
            '"confidence": 88, "reasoning": "4 连板板块最高+梯队完整+业绩预增", "risk": "高位放量分歧"}], '
            '"market_comment": "涨停 79 家情绪可"}'
        ))
        r = reason_dragons([_cand()], _ctx(), "B")
        assert r["primary"] == "601700"
        assert r["verdicts"][0]["confidence"] == 88
        # 确认走了 reasoning 路由（task_type 透传）
        # （call_llm mock 接收 **kw，task_type="reasoning" 由模块调用处给出）

    def test_llm_garbage_degrades(self, monkeypatch):
        monkeypatch.setattr(dr, "has_llm_api_key", lambda: True)
        monkeypatch.setattr(dr, "call_llm", lambda *a, **kw: "无法解析的回答")
        assert reason_dragons([_cand()], _ctx()) is None


class TestLlmClientK3Compat:
    def test_k3_temperature_coerced(self, monkeypatch):
        """kimi-k3 仅允许 temperature=1：call_llm 自动上调（否则 400）"""
        import shared.llm_client as lc

        captured = {}
        monkeypatch.setattr(lc, "_PROVIDER_KEYS", {"kimi": "sk-test", "deepseek": "", "custom": ""})
        monkeypatch.setattr(lc, "KIMI_MODEL", "kimi-k3")

        def fake_call(provider, prompt, system_prompt, temperature, max_tokens, model):
            captured.update(temperature=temperature, model=model)
            return "ok"

        monkeypatch.setattr(lc, "_call_provider", fake_call)
        r = lc.call_llm("测试", temperature=0.2, use_cache=False, task_type="summary")
        assert r == "ok"
        assert captured["temperature"] == 1.0
        assert captured["model"] == "kimi-k3"

    def test_non_k3_temperature_untouched(self, monkeypatch):
        """非 K3 模型温度保持原值"""
        import shared.llm_client as lc

        captured = {}
        monkeypatch.setattr(lc, "_PROVIDER_KEYS", {"kimi": "sk-test", "deepseek": "", "custom": ""})
        monkeypatch.setattr(lc, "KIMI_MODEL", "moonshot-v1-8k")
        monkeypatch.setattr(lc, "_call_provider",
                            lambda provider, prompt, system_prompt, temperature, max_tokens, model:
                            captured.update(temperature=temperature) or "ok")
        lc.call_llm("测试", temperature=0.3, use_cache=False, task_type="summary")
        assert captured["temperature"] == 0.3

    def test_cache_key_includes_model(self, monkeypatch, tmp_path):
        """同一 prompt 不同模型 → 不同缓存键（旧模型缓存不被误用）"""
        import shared.llm_client as lc

        monkeypatch.setattr(lc, "LLM_CACHE_DIR", tmp_path)
        monkeypatch.setattr(lc, "_PROVIDER_KEYS", {"kimi": "sk-test", "deepseek": "", "custom": ""})
        monkeypatch.setattr(lc, "_call_provider", lambda *a, **kw: "resp")
        monkeypatch.setattr(lc, "KIMI_MODEL", "model-a")
        lc.call_llm("同一提示词", task_type="summary")
        monkeypatch.setattr(lc, "KIMI_MODEL", "model-b")
        lc.call_llm("同一提示词", task_type="summary")
        assert len(list(tmp_path.glob("*.json"))) == 2


class TestSectorFocus:
    def test_sector_focus_parsed_with_guards(self):
        """sector_focus 解析：板块名须在梯队上下文内、持续性枚举校验"""
        text = ('{"primary": "601700", "verdicts": [], '
                '"sector_focus": ['
                '{"sector": "电网设备", "sustainability": "持续", "reason": "6家涨停2级梯队"},'
                '{"sector": "不存在板块", "sustainability": "持续", "reason": "x"},'
                '{"sector": "半导体", "sustainability": "永久", "reason": "y"}'
                '], "market_comment": ""}')
        r = _parse_verdicts(text, {"601700"}, ctx_sectors={"电网设备", "半导体"})
        assert len(r["sector_focus"]) == 2
        assert r["sector_focus"][0]["sector"] == "电网设备"
        assert r["sector_focus"][1]["sustainability"] == "一日"  # 非法值降级

    def test_no_sector_focus_ok(self):
        r = _parse_verdicts('{"primary": "", "verdicts": []}', {"601700"})
        assert r["sector_focus"] == []


class TestK26Temperature:
    def test_k26_temperature_coerced(self, monkeypatch):
        """kimi-k2.6 仅允许 temperature=1：自动上调"""
        import shared.llm_client as lc

        captured = {}
        monkeypatch.setattr(lc, "_PROVIDER_KEYS", {"kimi": "sk-test", "deepseek": "", "custom": ""})
        monkeypatch.setattr(lc, "KIMI_MODEL", "kimi-k2.6")
        monkeypatch.setattr(lc, "_call_provider",
                            lambda provider, prompt, system_prompt, temperature, max_tokens, model:
                            captured.update(temperature=temperature, model=model) or "ok")
        lc.call_llm("测试", temperature=0.2, use_cache=False, task_type="summary")
        assert captured["temperature"] == 1.0
        assert captured["model"] == "kimi-k2.6"


class TestPayloadReason:
    def test_reason_in_payload(self):
        """涨停归因进入推理输入（逻辑维度证据）"""
        c = _cand()
        c["reason"] = "电网+特高压"
        p = build_reasoning_payload([c], _ctx(), "B")
        assert "涨停归因：电网+特高压" in p["candidate_lines"]

    def test_no_reason_no_line(self):
        p = build_reasoning_payload([_cand()], _ctx(), "B")
        assert "涨停归因" not in p["candidate_lines"]
