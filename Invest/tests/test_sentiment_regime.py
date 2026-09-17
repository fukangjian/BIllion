"""
情绪周期状态机测试 — 纯函数相位判定 / 指标计算 / 闸门与竞价收紧，全部离线
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import SENTIMENT_BAN_PHASES
from pipeline.auction_check import (
    grade_hot_candidate,
    grade_position,
    grade_second_board,
)
from pipeline.second_board import build_second_board_pool
from pipeline.sentiment_regime import (
    classify_phase,
    compute_and_save,
    get_latest_phase,
    is_banned,
    metrics_from_rows,
    phase_advice,
    risk_multiplier,
    tightened_auction_params,
)
from review.compliance_check import check_market_conditions
from review.trade_log import Trade


# ---------- 指标计算 ----------

def _pool(date, symbol, pool_type="up", lbc=1, sector="半导体"):
    return {"trade_date": date, "symbol": symbol, "pool_type": pool_type,
            "lbc": lbc, "sector": sector, "name": f"股{symbol}"}


def _stats(limit_up=50, limit_down=5, broken=10):
    return {"limit_up_count": limit_up, "limit_down_count": limit_down, "broken_count": broken}


class TestMetrics:
    def test_basic_metrics(self):
        """涨停/炸板/高度/连板/晋级率口径正确"""
        today = [_pool("2026-09-16", f"60000{i}", lbc=1 if i < 4 else i) for i in range(6)]
        # 6 只涨停：4 首板 + 2 连板（2板、5板）；外加 3 只炸板
        today += [_pool("2026-09-16", f"00000{i}", pool_type="broken") for i in range(3)]
        prev = [_pool("2026-09-15", f"30000{i}", lbc=1) for i in range(10)]
        m = metrics_from_rows(_stats(limit_up=9, broken=3), today, prev)
        assert m["limit_up_count"] == 6
        assert m["broken_count"] == 3
        assert m["broken_rate"] == round(3 / 9 * 100, 1)
        assert m["max_lbc"] == 5
        assert m["lianban_count"] == 2
        assert m["prev_limit_up_count"] == 10
        assert m["promotion_rate"] == 20.0

    def test_missing_data_returns_none(self):
        """涨停池与 stats 双缺（抓取失败存 0）→ None，不误判冰点"""
        assert metrics_from_rows({"limit_up_count": 0, "broken_count": 0}, [], []) is None
        assert metrics_from_rows(None, [], []) is None

    def test_pool_empty_stats_fallback(self):
        """池为空但 stats 有涨停数 → 用 stats 兜底（晋级率等降级 None）"""
        m = metrics_from_rows(_stats(limit_up=45, broken=5), [], [])
        assert m["limit_up_count"] == 45
        assert m["promotion_rate"] is None
        assert m["max_lbc"] == 0


# ---------- 相位判定 ----------

class TestClassify:
    def test_icepoint(self):
        """极端悲观（涨停<30 / 跌停>50 / 炸板率≥60%）→ 冰点"""
        m = {"limit_up_count": 20, "limit_down_count": 60, "broken_count": 40,
             "broken_rate": 66.7, "max_lbc": 2, "lianban_count": 1,
             "promotion_rate": 5.0, "prev_limit_up_count": 45}
        phase, votes, reasons = classify_phase(m)
        assert phase == "冰点"
        assert votes["冰点"] >= 4

    def test_climax(self):
        """极度亢奋（涨停≥80 + 高度≥6）→ 高潮"""
        m = {"limit_up_count": 120, "limit_down_count": 2, "broken_count": 10,
             "broken_rate": 7.7, "max_lbc": 7, "lianban_count": 30,
             "promotion_rate": 45.0, "prev_limit_up_count": 100}
        phase, _, _ = classify_phase(m)
        assert phase == "高潮"

    def test_ebb(self):
        """退潮（炸板率≥40 + 晋级率<15 + 涨停大幅萎缩）→ 退潮"""
        m = {"limit_up_count": 40, "limit_down_count": 15, "broken_count": 30,
             "broken_rate": 42.9, "max_lbc": 5, "lianban_count": 3,
             "promotion_rate": 8.0, "prev_limit_up_count": 70}
        phase, _, _ = classify_phase(m)
        assert phase == "退潮"

    def test_ferment(self):
        """发酵（涨停居中上升 + 晋级率≥30 + 炸板率低）→ 发酵"""
        m = {"limit_up_count": 60, "limit_down_count": 5, "broken_count": 10,
             "broken_rate": 14.3, "max_lbc": 5, "lianban_count": 20,
             "promotion_rate": 35.0, "prev_limit_up_count": 45}
        phase, _, _ = classify_phase(m)
        assert phase == "发酵"

    def test_recovery_after_icepoint(self):
        """冰点后首日回暖（涨停回升）→ 修复（冰点后首日回暖是最佳买点）"""
        m = {"limit_up_count": 45, "limit_down_count": 10, "broken_count": 8,
             "broken_rate": 15.1, "max_lbc": 3, "lianban_count": 6,
             "promotion_rate": 25.0, "prev_limit_up_count": 22}
        phase, votes, _ = classify_phase(m, prev_phase="冰点")
        assert phase == "修复"
        assert votes["修复"] >= 5

    def test_none_metrics_unknown(self):
        phase, votes, _ = classify_phase(None)
        assert phase == "未知"
        assert votes == {}

    def test_tie_conservative(self):
        """平票保守优先：退潮票=发酵票时判退潮（宁可靠边风险相位）"""
        # 构造：炸板率 41（退潮+2）+ 晋级率 14（退潮+2）vs 发酵 0 票 → 退潮
        m = {"limit_up_count": 75, "limit_down_count": 3, "broken_count": 52,
             "broken_rate": 41.0, "max_lbc": 3, "lianban_count": 10,
             "promotion_rate": 14.0, "prev_limit_up_count": 75}
        phase, _, _ = classify_phase(m)
        assert phase == "退潮"


# ---------- 乘数 / 禁令 / 收紧口径 ----------

class TestGatingHelpers:
    def test_risk_multiplier(self):
        assert risk_multiplier("冰点") == 0.0
        assert risk_multiplier("退潮") == 0.0
        assert risk_multiplier("修复") == 0.5
        assert risk_multiplier("发酵") == 1.0
        assert risk_multiplier("高潮") == 0.5
        assert risk_multiplier(None) == 1.0
        assert risk_multiplier("未知") == 1.0

    def test_is_banned(self):
        for p in SENTIMENT_BAN_PHASES:
            assert is_banned(p)
        assert not is_banned("发酵")
        assert not is_banned(None)
        assert not is_banned("未知")

    def test_tightened_params_only_in_ban_phases(self):
        for p in ("退潮", "冰点"):
            t = tightened_auction_params(p)
            assert t and t["vol_ratio_good"] == 8.0 and t["hot_exec_open"] == (4.0, 6.0)
        assert tightened_auction_params("发酵") is None
        assert tightened_auction_params(None) is None
        assert tightened_auction_params("未知") is None


# ---------- 竞价收紧 ----------

class TestAuctionTighten:
    def test_hot_exec_tightened(self):
        """退潮期：高开 5% 竞昨比 6%（默认口径可执行）→ 收紧后仅观察"""
        verdict_default, _ = grade_hot_candidate(5.0, 6.0, False)
        verdict_tight, _ = grade_hot_candidate(5.0, 6.0, False, "退潮")
        assert verdict_default == "执行"
        assert verdict_tight == "观察"

    def test_hot_exec_window_tightened(self):
        """退潮期执行高开区间 3-7%→4-6%：高开 3.5% 即便放量也不执行"""
        verdict, _ = grade_hot_candidate(3.5, 9.0, False, "冰点")
        assert verdict == "观察"

    def test_sb_vol_tightened(self):
        """退潮期二板 A 级竞昨比 5%→8%：竞昨比 6% 从 A 级降为 B 级量不足"""
        grade_default, _ = grade_second_board(6.0, 6.0, False)
        grade_tight, _ = grade_second_board(6.0, 6.0, False, "退潮")
        assert grade_default == "A"
        assert grade_tight == "B"

    def test_position_low_open_tightened(self):
        """退潮期持仓低开警报线 -2%→-1%：-1.5% 触发警报"""
        assert grade_position(-1.5, 98.5, 95.0) is None
        alert = grade_position(-1.5, 98.5, 95.0, "退潮")
        assert alert and "盯防" in alert

    def test_default_phase_no_tighten(self):
        """发酵/None 相位分级与原口径一致（回归保护）"""
        assert grade_hot_candidate(5.0, 6.0, False, "发酵")[0] == grade_hot_candidate(5.0, 6.0, False)[0] == "执行"
        assert grade_second_board(6.0, 6.0, False, "发酵")[0] == grade_second_board(6.0, 6.0, False)[0] == "A"
        assert grade_position(-1.5, 98.5, 95.0, "发酵") is None


# ---------- 建仓闸门（情绪相位禁开超短仓） ----------

def _trade(system="HOT-S", risk=0.5):
    return Trade(股票代码="600519", 账户类型="事件", 入场系统=system,
                 入场价=100.0, 止损价=97.0, 风险率=risk, 股数=500, 仓位金额=50000)


class TestSentimentGate:
    def test_ban_hot_in_ebb(self):
        """退潮相位新开 HOT-S → 高级违规「情绪相位禁止建仓」"""
        vs = check_market_conditions(_trade(), [], "B", sentiment_phase="退潮")
        kinds = [(v.违规类型, v.严重程度) for v in vs]
        assert ("情绪相位禁止建仓", "高") in kinds

    def test_ban_evt_in_icepoint(self):
        vs = check_market_conditions(_trade(system="EVT-S"), [], "A", sentiment_phase="冰点")
        assert any(v.违规类型 == "情绪相位禁止建仓" and v.严重程度 == "高" for v in vs)

    def test_trend_not_banned_by_sentiment(self):
        """情绪闸门只管超短：S1-A 趋势仓不受退潮禁令（由 market_state D/C 管）"""
        vs = check_market_conditions(_trade(system="S1-A"), [], "B", sentiment_phase="退潮")
        assert not any(v.违规类型 == "情绪相位禁止建仓" for v in vs)

    def test_ferment_no_violation(self):
        vs = check_market_conditions(_trade(), [], "B", sentiment_phase="发酵")
        assert not any(v.违规类型 == "情绪相位禁止建仓" for v in vs)

    def test_none_phase_degrades(self):
        """相位 None/未知 → 不触发情绪闸门（降级，不阻塞建仓）"""
        for ph in (None, "未知"):
            vs = check_market_conditions(_trade(), [], None, sentiment_phase=ph)
            assert vs == []

    def test_market_state_none_still_checks_sentiment(self):
        """market_state 缺失时情绪闸门独立生效（两条门禁互不遮蔽）"""
        vs = check_market_conditions(_trade(), [], None, sentiment_phase="退潮")
        assert any(v.违规类型 == "情绪相位禁止建仓" for v in vs)


# ---------- DB 读写与编排 ----------

class TestComputeAndSave:
    def _db(self, tmp_path, prev_up_n=45):
        from pipeline.database import init_database, save_limit_pool, save_limit_stats
        import pandas as pd

        db = tmp_path / "market.db"
        init_database(db)
        # 前日：prev_up_n 只涨停 + 5 炸板；今日：发酵口径
        prev_rows = [_pool("2026-09-15", f"3000{i:02d}", lbc=1) for i in range(10)]
        prev_rows += [_pool("2026-09-15", f"3002{i:02d}", lbc=2) for i in range(prev_up_n - 10)]
        prev_rows += [_pool("2026-09-15", f"3001{i:02d}", pool_type="broken") for i in range(5)]
        save_limit_pool(pd.DataFrame(prev_rows), db_path=db)
        save_limit_stats(pd.DataFrame([{
            "trade_date": "2026-09-15", "limit_up_count": 45, "limit_down_count": 8,
            "broken_count": 5, "up_count": 2000, "down_count": 2500,
            "flat_count": 200, "total_amount": 7.5e11,
        }]), db_path=db)

        today_rows = [_pool("2026-09-16", f"6000{i:02d}", lbc=1) for i in range(40)]
        today_rows += [_pool("2026-09-16", f"6001{i:02d}", lbc=2) for i in range(15)]
        today_rows += [_pool("2026-09-16", "600199", lbc=5)] * 1
        today_rows += [_pool("2026-09-16", f"6002{i:02d}", pool_type="broken") for i in range(8)]
        save_limit_pool(pd.DataFrame(today_rows), db_path=db)
        save_limit_stats(pd.DataFrame([{
            "trade_date": "2026-09-16", "limit_up_count": 56, "limit_down_count": 3,
            "broken_count": 8, "up_count": 3000, "down_count": 1500,
            "flat_count": 300, "total_amount": 8.2e11,
        }]), db_path=db)
        return db

    def test_compute_save_and_get_latest(self, tmp_path):
        db = self._db(tmp_path)
        row = compute_and_save("2026-09-16", db_path=db)
        assert row is not None
        assert row["phase"] == "发酵"
        assert row["prev_phase"] is None  # 库中尚无前日相位记录
        assert row["promotion_rate"] == round(16 / 45 * 100, 1)

        latest = get_latest_phase(db_path=db)
        assert latest and latest["phase"] == "发酵"
        assert latest["trading_allowed"] is True
        assert latest["risk_mult"] == 1.0

    def test_prev_phase_chain(self, tmp_path):
        """连续两日判定 → 第二日携带 prev_phase（修复判定依赖）"""
        db = self._db(tmp_path, prev_up_n=10)
        compute_and_save("2026-09-15", db_path=db)  # 前日池 10 涨停 → 冰点
        row = compute_and_save("2026-09-16", db_path=db)
        assert row["prev_phase"] == "冰点"

    def test_missing_day_returns_none(self, tmp_path):
        from pipeline.database import init_database

        db = tmp_path / "empty.db"
        init_database(db)
        assert compute_and_save("2026-09-16", db_path=db) is None
        assert get_latest_phase(db_path=db) is None

    def test_pre_market_fallback_to_latest_pool_date(self, tmp_path):
        """盘前运行（当日池未生成）→ 回退最近一期池数据，相位行落在数据日而非请求日"""
        db = self._db(tmp_path)
        row = compute_and_save("2026-09-17", db_path=db)  # 库中只有 09-15/09-16 池
        assert row is not None
        assert row["trade_date"] == "2026-09-16"
        assert row["phase"] == "发酵"
        latest = get_latest_phase(db_path=db)
        assert latest["trade_date"] == "2026-09-16"


# ---------- 二板池情绪调制 ----------

class TestSecondBoardSentiment:
    def _mk_db(self, tmp_path, phase: str):
        """库中写入指定情绪相位（复用 build_second_board_pool 的读取路径）"""
        from pipeline.database import init_database, save_emotion_state

        db = tmp_path / "sb.db"
        init_database(db)
        save_emotion_state({
            "trade_date": "2026-09-16", "phase": phase, "limit_up_count": 50,
            "limit_down_count": 5, "broken_count": 10, "broken_rate": 16.7,
            "max_lbc": 5, "lianban_count": 12, "promotion_rate": 30.0,
            "prev_phase": None, "notes": "test",
        }, db_path=db)
        return db

    def test_ebb_caps_pool_and_notes(self, tmp_path, monkeypatch):
        """退潮相位：观察池缩至前 3 只并标注禁开新仓"""
        import pipeline.second_board as sb
        import pandas as pd

        db = self._mk_db(tmp_path, "退潮")
        cands = [
            {"symbol": f"600{i:03d}", "name": f"股{i}", "sector": "半导体", "score": 8 - i,
             "score_notes": [], "close": 20.0, "fbt": "09:31:00", "seal_ratio": 2.0,
             "turnover": 5.0, "cap_yi": 60.0, "prev5_gain": 3.0,
             "limit_up_price": 22.0, "stop_price": 18.0, "on_lhb": False}
            for i in range(5)
        ]
        monkeypatch.setattr(sb, "screen_first_boards", lambda *a, **k: (cands, {}, 0))
        monkeypatch.setattr(sb, "_load_lhb_net", lambda *a, **k: {})
        monkeypatch.setattr("pipeline.database.load_limit_pool", lambda *a, **k: pd.DataFrame({
            "trade_date": ["2026-09-16"] * 3, "symbol": ["600000", "600001", "600002"],
            "name": ["a", "b", "c"], "pool_type": ["up"] * 3, "lbc": [1, 1, 1],
        }))
        result = build_second_board_pool("2026-09-16", db_path=db)
        assert len(result["candidates"]) == 3
        assert result["sentiment"]["phase"] == "退潮"
        assert result["sentiment"]["trading_allowed"] is False
        assert "禁开新仓" in result["note"]

    def test_ferment_keeps_top_n(self, tmp_path, monkeypatch):
        """发酵相位：按默认 SECOND_BOARD_TOP_N 正常（不缩池）"""
        import pipeline.second_board as sb
        import pandas as pd

        db = self._mk_db(tmp_path, "发酵")
        cands = [
            {"symbol": f"600{i:03d}", "name": f"股{i}", "sector": "半导体", "score": 8 - i,
             "score_notes": [], "close": 20.0, "fbt": "09:31:00", "seal_ratio": 2.0,
             "turnover": 5.0, "cap_yi": 60.0, "prev5_gain": 3.0,
             "limit_up_price": 22.0, "stop_price": 18.0, "on_lhb": False}
            for i in range(10)
        ]
        monkeypatch.setattr(sb, "screen_first_boards", lambda *a, **k: (cands, {}, 0))
        monkeypatch.setattr(sb, "_load_lhb_net", lambda *a, **k: {})
        monkeypatch.setattr("pipeline.database.load_limit_pool", lambda *a, **k: pd.DataFrame({
            "trade_date": ["2026-09-16"] * 3, "symbol": ["600000", "600001", "600002"],
            "name": ["a", "b", "c"], "pool_type": ["up"] * 3, "lbc": [1, 1, 1],
        }))
        from config import SECOND_BOARD_TOP_N

        result = build_second_board_pool("2026-09-16", db_path=db)
        assert len(result["candidates"]) == min(SECOND_BOARD_TOP_N, 10)


# ---------- 展示口径 ----------

def test_phase_advice_exists_for_all_phases():
    from config import SENTIMENT_PHASES

    for p in SENTIMENT_PHASES:
        assert phase_advice(p)
    assert phase_advice(None)
