"""
盘前操作清单测试 — mock 扫描 JSON / 监控结果 + 临时 trades/db，不依赖网络、不碰真实数据
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.daily_plan import build_daily_plan, daily_plan_to_markdown
from review.trade_log import TradeLog


def _scan_json(s1a=None, s2a=None, market_state="B"):
    return {
        "date": "2026-08-06",
        "market_state": market_state,
        "breakout_s1a": s1a or [],
        "breakout_s2a": s2a or [],
        "sector_ranking": [],
        "hot_pool": {},
    }


def _cand(symbol="600519", close=100.0, atr=2.0, passed=3, required=3,
          record=True, note="", breakout_pct=2.0):
    return {
        "symbol": symbol, "close": close, "channel_high": close * 0.98,
        "breakout_pct": breakout_pct, "atr_20": atr, "period": 20,
        "filter_passed": passed, "filters_required": required,
        "filter_brief": "周线✓ 板块✓ 量能✓", "note": note, "record": record,
    }


def _monitor(alerts=None, state="Normal"):
    return {"drawdown_state": {"state": state}, "alerts": alerts or []}


def _tmp_log(tmp_path) -> TradeLog:
    f = tmp_path / "trades.json"
    f.write_text("[]", encoding="utf-8")
    return TradeLog(f)


def _tmp_db(tmp_path) -> Path:
    """空库（无 market_state 行 → 闸门市场状态检查降级跳过）"""
    from pipeline.database import init_database

    db = tmp_path / "market.db"
    init_database(db)
    return db


class TestBuyCandidates:
    def test_all_passed_included_with_params(self, tmp_path):
        """滤网 3/3 全过 → 入清单：止损=收盘−2×ATR，股数/风险率来自 calc_position 口径"""
        plan = build_daily_plan(
            _scan_json(s1a=[_cand()]), _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert len(plan["trend_buys"]) == 1
        b = plan["trend_buys"][0]
        assert b["symbol"] == "600519"
        assert b["system"] == "S1-A"
        assert b["stop"] == pytest.approx(96.0)       # 100 − 2×2
        assert b["shares"] == 200                     # 预算 1000 / 每股风险 4 → 250 → 整手 200
        assert b["risk_pct"] == pytest.approx(1.0)    # 产业 Normal 风险率上限
        assert b["account"] == "产业"
        assert b["gate"] == "通过"

    def test_not_all_passed_excluded(self, tmp_path):
        """滤网 2/3 → 不入清单（仅观察级）"""
        plan = build_daily_plan(
            _scan_json(s1a=[_cand(passed=2)]), _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert plan["trend_buys"] == []

    def test_cooldown_record_false_excluded(self, tmp_path):
        """冷却中（record=False）→ 不入清单"""
        plan = build_daily_plan(
            _scan_json(s1a=[_cand(record=False, note="冷却中（连续假突破×3）")]), _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert plan["trend_buys"] == []

    def test_cap_and_order_by_breakout(self, tmp_path):
        """候选上限 5 只，按突破幅度降序保留"""
        cands = [_cand(symbol=f"6000{i:02d}", breakout_pct=float(i)) for i in range(7)]
        plan = build_daily_plan(
            _scan_json(s1a=cands), _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        buys = plan["trend_buys"]
        assert len(buys) == 5
        assert [b["symbol"] for b in buys] == ["600006", "600005", "600004", "600003", "600002"]

    def test_chinext_allowed_after_ban_lifted(self, tmp_path):
        """创业板候选（300）→ 2026-08-07 放开后闸门预检不再报禁买板块"""
        plan = build_daily_plan(
            _scan_json(s1a=[_cand(symbol="300750")]), _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert len(plan["trend_buys"]) == 1
        assert "禁买板块" not in plan["trend_buys"][0]["gate"]

    def test_s2a_account_mapping(self, tmp_path):
        """S2-A 候选默认账户为核心"""
        plan = build_daily_plan(
            _scan_json(s2a=[_cand(passed=4, required=4)]), _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert plan["trend_buys"][0]["account"] == "核心"

    def test_same_symbol_dual_system_dedup_prefers_s2a(self, tmp_path):
        """同股同时出现 S1-A/S2-A 信号 → 去重保留 S2-A（与 from-scan 默认一致）"""
        plan = build_daily_plan(
            _scan_json(s1a=[_cand(symbol="601899")], s2a=[_cand(symbol="601899", passed=4, required=4)]),
            _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert len(plan["trend_buys"]) == 1
        assert plan["trend_buys"][0]["system"] == "S2-A"


def _dragon(symbol="601700", name="风范股份", grade="S", score=85, close=6.51, atr=0.26):
    return {
        "symbol": symbol, "name": name, "close": close, "channel_high": close * 0.9,
        "breakout_pct": 10.0, "atr_20": atr, "period": 20, "source": "连板",
        "sector": "电网设备", "lbc": 4, "dragon_grade": grade, "dragon_score": score,
        "dragon_dims": {"身位": 30, "梯队": 20, "强度": 15, "逻辑": 20, "情绪": 10},
        "analysis": "符合 4 条（①③④⑧）",
    }


class TestDragonBuys:
    def test_dragon_candidates_included_with_params(self, tmp_path):
        """龙头候选入清单：HOT-S / 事件账户 / 止损=收盘−2×ATR / 等级与五维携带"""
        scan = _scan_json()
        scan["hot_pool"] = {"dragon_candidates": [_dragon()]}
        plan = build_daily_plan(
            scan, _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert len(plan["dragon_buys"]) == 1
        b = plan["dragon_buys"][0]
        assert b["system"] == "HOT-S"
        assert b["account"] == "事件"
        assert b["grade"] == "S" and b["score"] == 85
        assert b["stop"] == pytest.approx(6.51 - 0.52)  # 6.51 − 2×0.26
        assert b["dims"]["身位"] == 30

    def test_no_dragon_only_trend(self, tmp_path):
        """无龙头候选时 dragon_buys 为空、趋势组照常"""
        plan = build_daily_plan(
            _scan_json(s1a=[_cand()]), _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert plan["dragon_buys"] == []
        assert len(plan["trend_buys"]) == 1

    def test_markdown_dragon_before_trend(self, tmp_path):
        """报告龙头组在趋势组之前，两组标题并存"""
        scan = _scan_json(s1a=[_cand()])
        scan["hot_pool"] = {"dragon_candidates": [_dragon()]}
        plan = build_daily_plan(
            scan, _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        md = "\n".join(daily_plan_to_markdown(plan))
        i_dragon = md.index("🐉 龙头候选")
        i_trend = md.index("📈 趋势候选")
        assert i_dragon < i_trend
        assert "HOT-S" in md

    def test_verdict_priority_ordering(self, tmp_path):
        """系统判定排序：真龙头排前（即使量化分低于疑似龙头）"""
        d1 = _dragon(symbol="600001", name="高分疑似", grade="S", score=90)
        d1["verdict"] = "疑似龙头"
        d1["confidence"] = 60
        d2 = _dragon(symbol="600002", name="低分真龙", grade="A", score=70)
        d2["verdict"] = "真龙头"
        d2["confidence"] = 80
        scan = _scan_json()
        scan["hot_pool"] = {"dragon_candidates": [d1, d2], "dragon_primary": "600002"}
        plan = build_daily_plan(
            scan, _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert [b["symbol"] for b in plan["dragon_buys"]] == ["600002", "600001"]
        assert plan["dragon_primary"] == "600002"
        md = "\n".join(daily_plan_to_markdown(plan))
        assert "本期系统认定龙头：低分真龙（600002）" in md

    def test_dragon_note_passthrough(self, tmp_path):
        """K3 推理降级标注透传到计划（UI 据此显示警告横幅）"""
        scan = _scan_json()
        scan["hot_pool"] = {
            "dragon_candidates": [_dragon()],
            "dragon_note": "⚠️ K3 深度推理未启用（未配置 LLM Key 或已禁用），当前按量化评分排序",
        }
        plan = build_daily_plan(
            scan, _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert "K3 深度推理未启用" in plan["dragon_note"]


class TestPositionActions:
    def test_alerts_become_actions(self, tmp_path):
        """监控警报（含无止损）逐条转持仓行动"""
        alerts = [{
            "交易编号": "T1", "股票代码": "605388", "股票名称": "均瑶健康",
            "类型": "无止损", "现价": 5.97,
            "建议动作": "该持仓无止损价，监控保护失效：立即补设止损（update --stop）或卖出",
        }]
        plan = build_daily_plan(
            _scan_json(), _monitor(alerts=alerts),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert len(plan["position_actions"]) == 1
        assert plan["position_actions"][0]["类型"] == "无止损"
        assert "补设止损" in plan["position_actions"][0]["行动"]


class TestNoTradeConditions:
    def test_state_d_forbids_new_trend(self, tmp_path):
        plan = build_daily_plan(
            _scan_json(market_state="D"), _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert any("禁止新开趋势仓" in c for c in plan["no_trade_conditions"])

    def test_state_c_half_risk(self, tmp_path):
        plan = build_daily_plan(
            _scan_json(market_state="C"), _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert any("风险减半" in c for c in plan["no_trade_conditions"])

    def test_cooling_rules_always_present(self, tmp_path):
        plan = build_daily_plan(
            _scan_json(), _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert any("30 分钟规则" in c for c in plan["no_trade_conditions"])


class TestMarkdown:
    def test_render_full(self, tmp_path):
        plan = build_daily_plan(
            _scan_json(s1a=[_cand()], market_state="C"),
            _monitor(alerts=[{"交易编号": "T1", "股票代码": "605388", "股票名称": "均瑶健康",
                              "类型": "无止损", "现价": 5.97, "建议动作": "补设止损"}]),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        md = "\n".join(daily_plan_to_markdown(plan))
        assert "## 二、明日操作计划" in md
        assert "买入候选（1）" in md
        assert "600519" in md
        assert "持仓行动（1）" in md
        assert "无止损" in md
        assert "不交易条件" in md

    def test_render_empty(self, tmp_path):
        plan = build_daily_plan(
            _scan_json(), _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        md = "\n".join(daily_plan_to_markdown(plan))
        assert "不交易也是操作" in md
        assert "按原计划持有" in md


class TestBidChecklist:
    def test_bid_checklist_built(self, tmp_path):
        """9:25 竞价核对清单随龙头候选生成，含执行参数"""
        scan = _scan_json()
        scan["hot_pool"] = {"dragon_candidates": [_dragon()]}
        plan = build_daily_plan(
            scan, _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert len(plan["bid_checklist"]) == 1
        item = plan["bid_checklist"][0]
        assert item["symbol"] == "601700"
        assert "高开 3%~5%" in item["text"]
        assert "一字板" in item["text"]
        md = "\n".join(daily_plan_to_markdown(plan))
        assert "9:25 竞价核对清单" in md

    def test_sector_focus_passthrough(self, tmp_path):
        """明日板块聚焦透传并渲染"""
        scan = _scan_json()
        scan["hot_pool"] = {
            "dragon_candidates": [_dragon()],
            "sector_focus": [{"sector": "电网设备", "sustainability": "持续", "reason": "6家涨停2级梯队"}],
        }
        plan = build_daily_plan(
            scan, _monitor(),
            equity=100_000, trade_log=_tmp_log(tmp_path), db_path=_tmp_db(tmp_path),
        )
        assert plan["sector_focus"][0]["sustainability"] == "持续"
        md = "\n".join(daily_plan_to_markdown(plan))
        assert "明日板块聚焦" in md and "电网设备（持续）" in md
