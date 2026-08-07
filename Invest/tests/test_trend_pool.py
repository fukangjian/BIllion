"""
趋势动态池测试 — 合并去重/剔除创业板/截断/入库读取；mock 抓取，不依赖网络、不碰真实 market.db
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.database import get_connection, init_database, load_trend_pool
from pipeline.trend_pool import _merge_trend_pool, build_trend_pool


def _cons(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    """构造成分股原料：(代码, 名称, 来源板块)"""
    return pd.DataFrame(rows, columns=["symbol", "name", "source_sector"])


class TestMergeTrendPool:
    def test_dedupe_and_join_sectors(self):
        """同股出现在两个板块 → 去重，板块名以「+」连接（保留强度最高板块的首次位置）"""
        cons = _cons([
            ("600519", "贵州茅台", "酿酒"),
            ("600519", "贵州茅台", "食品饮料"),
            ("601318", "中国平安", "保险"),
        ])
        pool = _merge_trend_pool(cons)
        assert len(pool) == 2
        row = pool[pool["symbol"] == "600519"].iloc[0]
        assert row["source_sector"] == "酿酒+食品饮料"
        assert pool.iloc[0]["symbol"] == "600519"  # 顺序保持输入序

    def test_chinext_kept_after_ban_lifted(self):
        """2026-08-07 放开创业板：300/301 正常保留入池"""
        cons = _cons([
            ("300750", "宁德时代", "电池"),
            ("301234", "某创业", "电池"),
            ("688235", "百济神州", "创新药"),
            ("000858", "五粮液", "酿酒"),
        ])
        pool = _merge_trend_pool(cons)
        assert set(pool["symbol"]) == {"300750", "301234", "688235", "000858"}

    def test_truncate_to_max(self, monkeypatch):
        """池上限截断（按板块强度输入序优先）"""
        import pipeline.trend_pool as tp

        monkeypatch.setattr(tp, "TREND_POOL_MAX", 3)
        cons = _cons([(f"6000{i:02d}", f"股{i}", "板块A") for i in range(10)])
        pool = _merge_trend_pool(cons)
        assert len(pool) == 3
        assert pool["symbol"].tolist() == ["600000", "600001", "600002"]

    def test_empty_input(self):
        pool = _merge_trend_pool(pd.DataFrame())
        assert pool.empty
        assert list(pool.columns) == ["symbol", "name", "source_sector"]

    def test_symbol_normalized(self):
        """带后缀代码归一化为 6 位数字"""
        cons = _cons([("600519.SH", "贵州茅台", "酿酒")])
        pool = _merge_trend_pool(cons)
        assert pool.iloc[0]["symbol"] == "600519"


class TestBuildTrendPool:
    def test_build_and_load_latest(self, tmp_path, monkeypatch):
        """构建写库 + load_trend_pool 取最新一期（全程 mock，不联网）"""
        import pipeline.trend_pool as tp

        monkeypatch.setattr(
            tp, "strong_sectors_ranked",
            lambda db_path=None: pd.DataFrame({
                "sector_name": ["半导体", "创新药", "酿酒"],
                "rank": [1, 2, 3],
            }),
        )
        monkeypatch.setattr(
            tp, "_fetch_constituents_for_sectors",
            lambda sectors: _cons([
                ("688981", "中芯国际", "半导体"),
                ("300001", "某创业", "半导体"),  # 2026-08-07 放开创业板后正常入池
                ("688235", "百济神州", "创新药"),
            ]),
        )

        db = tmp_path / "market.db"
        pool = build_trend_pool(trade_date="2026-08-05", db_path=db, top_n=2)
        assert set(pool["symbol"]) == {"688981", "300001", "688235"}

        loaded = load_trend_pool(db_path=db)
        assert set(loaded["symbol"]) == {"688981", "300001", "688235"}
        assert set(loaded["trade_date"]) == {"2026-08-05"}

        # 再写一期更新日期，load 默认取最新
        pool2 = pool.copy()
        pool2["trade_date"] = "2026-08-06"
        from pipeline.database import save_trend_pool
        save_trend_pool(pool2, db)
        loaded2 = load_trend_pool(db_path=db)
        assert set(loaded2["trade_date"]) == {"2026-08-06"}
        # 指定日期仍可取旧期
        loaded_old = load_trend_pool(trade_date="2026-08-05", db_path=db)
        assert set(loaded_old["trade_date"]) == {"2026-08-05"}

    def test_empty_ranking_returns_empty(self, tmp_path, monkeypatch):
        """板块排名为空 → 返回空表且不写库"""
        import pipeline.trend_pool as tp

        monkeypatch.setattr(tp, "strong_sectors_ranked", lambda db_path=None: pd.DataFrame())
        db = tmp_path / "market.db"
        pool = build_trend_pool(trade_date="2026-08-05", db_path=db)
        assert pool.empty
        init_database(db)
        with get_connection(db) as conn:
            n = conn.execute("SELECT COUNT(*) FROM trend_pool").fetchone()[0]
        assert n == 0


# ---------- 成分股双源 fallback（同花顺优先，东财备用） ----------

class TestFetchSectorConstituentsFallback:
    def test_ths_primary_used_when_available(self, monkeypatch):
        """同花顺有数据时直接返回，不调东财"""
        import shared.data_fetcher as df_mod

        ths_df = pd.DataFrame({"symbol": ["600519"], "name": ["贵州茅台"]})
        monkeypatch.setattr(df_mod, "_fetch_sector_constituents_ths", lambda name: ths_df)

        def _em_should_not_run(name):  # pragma: no cover - 断言用
            raise AssertionError("不应调用东财")

        monkeypatch.setattr(df_mod, "_fetch_sector_constituents_em", _em_should_not_run)
        out = df_mod.fetch_sector_constituents("白酒")
        assert out["symbol"].tolist() == ["600519"]

    def test_em_fallback_when_ths_empty(self, monkeypatch):
        """同花顺为空 → 自动降级东财"""
        import shared.data_fetcher as df_mod

        monkeypatch.setattr(df_mod, "_fetch_sector_constituents_ths",
                            lambda name: pd.DataFrame(columns=["symbol", "name"]))
        monkeypatch.setattr(df_mod, "_fetch_sector_constituents_em",
                            lambda name: pd.DataFrame({"symbol": ["000858"], "name": ["五粮液"]}))
        out = df_mod.fetch_sector_constituents("白酒")
        assert out["symbol"].tolist() == ["000858"]

    def test_both_fail_returns_empty(self, monkeypatch):
        """双源全失败 → 空表（调用方降级跳过该板块，不阻塞）"""
        import shared.data_fetcher as df_mod

        empty = pd.DataFrame(columns=["symbol", "name"])
        monkeypatch.setattr(df_mod, "_fetch_sector_constituents_ths", lambda name: empty)
        monkeypatch.setattr(df_mod, "_fetch_sector_constituents_em", lambda name: empty.copy())
        out = df_mod.fetch_sector_constituents("不存在的板块")
        assert out.empty
