"""
龙头评分测试 — 临时 SQLite + 纯函数真值表，不依赖网络（《如何识别真假龙头》三维验证口径）
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.database import get_connection, init_database
from pipeline.dragon_head import (
    build_dragon_context,
    dragon_top,
    grade_hot_candidates,
    score_dragon,
)


def _ctx(sectors=None, limit_up=60):
    return {
        "trade_date": "2026-08-06",
        "sectors": sectors or {},
        "emotion": {"limit_up_count": limit_up, "up_count": 3000, "down_count": 2000},
    }


def _sec(count=6, max_lbc=3, leaders=("601700",), fbts=None):
    lbcs = [max_lbc] + [1] * (count - 1)
    return {"count": count, "max_lbc": max_lbc, "lbcs": lbcs,
            "levels": len(set(lbcs)), "leaders": list(leaders),
            "fbts": sorted(fbts or ["093000", "093100", "093200", "100000"])}


def _rec(**kw) -> dict:
    r = {
        "symbol": "601700", "sector": "电网设备", "lbc": 3,
        "fbt": "093000", "turnover": 20.0, "seal_amount": 1.2e8, "zbc": 0,
        "catalyst": {"satisfied": True, "catalyst_type": "业绩/事件"},
    }
    r.update(kw)
    return r


class TestScorePosition:
    def test_sector_highest_lbc(self):
        """板块最高连板（lbc≥2 且为 leaders）→ 身位 30"""
        ctx = _ctx({"电网设备": _sec(max_lbc=3)})
        r = score_dragon(_rec(), ctx)
        assert r["dims"]["身位"] == 30

    def test_first_board_early_seal(self):
        """首板且封板时间板块前 3 → 身位 20"""
        ctx = _ctx({"电网设备": _sec(max_lbc=3)})
        r = score_dragon(_rec(lbc=1, symbol="600000", fbt="093000"), ctx)
        assert r["dims"]["身位"] == 20

    def test_follower(self):
        """lbc 低于板块最高 → 跟风 8"""
        ctx = _ctx({"电网设备": _sec(max_lbc=4)})
        r = score_dragon(_rec(lbc=2, symbol="600000"), ctx)
        assert r["dims"]["身位"] == 8


class TestScoreEchelon:
    def test_full_echelon(self):
        ctx = _ctx({"电网设备": _sec(count=6, max_lbc=3)})
        assert score_dragon(_rec(), ctx)["dims"]["梯队"] == 20

    def test_thin_sector(self):
        """板块仅 1 家涨停 → 独木难支 4 分"""
        ctx = _ctx({"电网设备": _sec(count=1, max_lbc=1)})
        assert score_dragon(_rec(lbc=1, symbol="601700"), ctx)["dims"]["梯队"] == 4


class TestScoreStrength:
    def test_healthy_turnover_and_seal(self):
        """换手 20%（15-35 健康区）+ 封单 1.2 亿 → 10+10=20"""
        ctx = _ctx({"电网设备": _sec()})
        assert score_dragon(_rec(), ctx)["dims"]["强度"] == 20

    def test_one_word_board_penalty(self):
        """一字板 → 强度 4（无量，要么买不到）"""
        ctx = _ctx({"电网设备": _sec()})
        assert score_dragon(_rec(one_word_board=True), ctx)["dims"]["强度"] == 4

    def test_repeated_broken_halves(self):
        """炸板 ≥3 次 → 强度减半"""
        ctx = _ctx({"电网设备": _sec()})
        r = score_dragon(_rec(zbc=3), ctx)
        assert r["dims"]["强度"] == 10  # 20 // 2
        assert any("减半" in n for n in r["notes"])


class TestScoreLogicAndEmotion:
    def test_logic_branches(self):
        ctx = _ctx({"电网设备": _sec()})
        assert score_dragon(_rec(), ctx)["dims"]["逻辑"] == 20
        assert score_dragon(_rec(catalyst={"satisfied": False}), ctx)["dims"]["逻辑"] == 0
        assert score_dragon(_rec(catalyst=None), ctx)["dims"]["逻辑"] == 8

    def test_emotion_tiers(self):
        sec = _sec()
        assert score_dragon(_rec(), _ctx({"电网设备": sec}, limit_up=60))["dims"]["情绪"] == 10
        assert score_dragon(_rec(), _ctx({"电网设备": sec}, limit_up=35))["dims"]["情绪"] == 6
        assert score_dragon(_rec(), _ctx({"电网设备": sec}, limit_up=22))["dims"]["情绪"] == 3
        assert score_dragon(_rec(), _ctx({"电网设备": sec}, limit_up=10))["dims"]["情绪"] == 0


class TestGrade:
    def test_grade_boundaries(self):
        sec = _sec()
        base = _ctx({"电网设备": sec})
        # 满分组合 30+20+20+20+10=100 → S
        assert score_dragon(_rec(), base)["grade"] == "S"
        # 跟风+独木+一字+无催化+冰点 = 8+4+4+0+0=16 → C
        weak_ctx = _ctx({"电网设备": _sec(count=1, max_lbc=1)}, limit_up=10)
        r = score_dragon(_rec(symbol="600000", lbc=1, one_word_board=True,
                              catalyst={"satisfied": False}), weak_ctx)
        assert r["grade"] == "C"


class TestContextFromDb:
    def test_build_context_and_migration(self, tmp_path):
        """旧表（无新列）经 init_database 迁移后可读写新字段；梯队/情绪聚合正确"""
        db = tmp_path / "market.db"
        init_database(db)
        with get_connection(db) as conn:  # 模拟旧库：删新列无法直接做，改验证迁移幂等
            conn.execute(
                "INSERT INTO limit_pool (trade_date, symbol, name, pool_type, change_pct, "
                "amount, lbc, sector, fbt, seal_amount, turnover, zbc) VALUES "
                "('2026-08-06','601700','风范股份','up',10.0,1e8,4,'电网设备','093000',1.2e8,20.0,0)"
            )
            conn.execute(
                "INSERT INTO limit_pool (trade_date, symbol, name, pool_type, change_pct, "
                "amount, lbc, sector, fbt, seal_amount, turnover, zbc) VALUES "
                "('2026-08-06','600001','跟风股','up',10.0,5e7,1,'电网设备','100500',3e7,25.0,0)"
            )
            conn.execute(
                "INSERT INTO limit_stats (trade_date, limit_up_count, up_count, down_count) "
                "VALUES ('2026-08-06', 79, 3000, 2000)"
            )
        ctx = build_dragon_context("2026-08-06", db_path=db)
        sec = ctx["sectors"]["电网设备"]
        assert sec["count"] == 2 and sec["max_lbc"] == 4
        assert sec["leaders"] == ["601700"]
        assert ctx["emotion"]["limit_up_count"] == 79

    def test_grade_and_top(self, tmp_path):
        """grade_hot_candidates 附加字段；dragon_top 只留 S/A/B 且按分数降序"""
        ctx = _ctx({"电网设备": _sec()})
        records = [_rec(), _rec(symbol="600000", lbc=1, fbt="110000",
                                catalyst={"satisfied": False})]
        grade_hot_candidates(records, ctx)
        assert "dragon_score" in records[0]
        top = dragon_top(records)
        assert top[0]["symbol"] == "601700"
        assert all(r["dragon_grade"] in ("S", "A", "B") for r in top)


class TestNanSafety:
    def test_nan_fields_do_not_crash(self):
        """老库新列 NULL → pandas NaN：评分不崩（回归：int(NaN) ValueError 曾致龙头候选全空）"""
        ctx = _ctx({"电网设备": _sec()})
        r = score_dragon(_rec(lbc=float("nan"), turnover=float("nan"),
                              seal_amount=float("nan"), zbc=float("nan")), ctx)
        assert r["grade"] in ("S", "A", "B", "C")
        assert r["dims"]["强度"] >= 0

    def test_to_int_to_float_nan(self):
        from pipeline.dragon_head import to_float, to_int

        assert to_int(float("nan")) == 0
        assert to_float(float("nan")) == 0.0
        assert to_int(None) == 0
        assert to_float(None) == 0.0
        assert to_int("4") == 4
        assert to_float("2.5") == 2.5
