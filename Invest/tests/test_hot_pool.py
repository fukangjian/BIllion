"""
超短热点池测试 — mock akshare + 临时 SQLite，不依赖网络、不碰真实 data/market.db
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import HOT_SIGNAL_SYSTEM
from pipeline.database import (
    init_database,
    load_hot_pool,
    load_limit_pool,
    save_daily_quotes,
    save_hot_pool,
    save_limit_pool,
)
from pipeline.hot_pool import _merge_pool
from pipeline.signal_tracker import record_signals, settle_signals, signal_stats
from shared.data_fetcher import fetch_limit_pools


# ---------- 测试数据构造 ----------

def _zt_df() -> pd.DataFrame:
    return pd.DataFrame([{
        "序号": 1, "代码": "002827", "名称": "高争民爆", "涨跌幅": 10.0,
        "最新价": 31.45, "成交额": 52276190, "连板数": 2, "所属行业": "化学制品",
    }])


def _zb_df() -> pd.DataFrame:
    return pd.DataFrame([{
        "序号": 1, "代码": "600111", "名称": "测试炸板", "涨跌幅": 5.5,
        "最新价": 10.0, "成交额": 1000, "所属行业": "半导体",
    }])


def _limit_rows() -> pd.DataFrame:
    return pd.DataFrame([
        {"symbol": "000001", "name": "平安银行", "pool_type": "up", "change_pct": 10.0, "amount": 1.0, "lbc": 3, "sector": "银行"},
        {"symbol": "000002", "name": "万科A", "pool_type": "up", "change_pct": 9.9, "amount": 1.0, "lbc": 1, "sector": "地产"},
        {"symbol": "000003", "name": "测试炸板", "pool_type": "broken", "change_pct": 5.0, "amount": 1.0, "lbc": 0, "sector": "半导体"},
    ])


def _q(d: str, low: float, close: float) -> dict:
    return {
        "trade_date": d,
        "open": (low + close) / 2,
        "high": max(low, close) + 0.5,
        "low": low,
        "close": close,
        "volume": 1000.0,
        "amount": 100000.0,
    }


def _flat_rows(start: str, days: int) -> list[dict]:
    """横盘日线：low=99 close=100 high=100.5（不触止损 98、不破通道）"""
    dates = pd.date_range(start, periods=days, freq="D").strftime("%Y-%m-%d")
    return [_q(d, 99.0, 100.0) for d in dates]


def _make_db(tmp_path, rows_by_symbol: dict[str, list[dict]]) -> Path:
    db = tmp_path / "market.db"
    init_database(db)
    for sym, rows in rows_by_symbol.items():
        df = pd.DataFrame(rows)
        df["symbol"] = sym
        save_daily_quotes(
            df[["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]],
            db,
        )
    return db


# ---------- fetch_limit_pools 字段映射（mock akshare，离线） ----------

def test_fetch_limit_pools_mapping(monkeypatch):
    import akshare as ak

    monkeypatch.setattr(ak, "stock_zt_pool_em", lambda date=None: _zt_df())
    monkeypatch.setattr(ak, "stock_zt_pool_zbgc_em", lambda date=None: _zb_df())

    df = fetch_limit_pools("2026-07-31")
    assert len(df) == 2
    up = df[df["pool_type"] == "up"].iloc[0]
    assert up["symbol"] == "002827"
    assert up["lbc"] == 2
    assert up["sector"] == "化学制品"
    broken = df[df["pool_type"] == "broken"].iloc[0]
    assert broken["symbol"] == "600111"
    assert broken["lbc"] == 0


def test_fetch_limit_pools_partial_failure(monkeypatch):
    """涨停池失败不阻塞炸板池（重试降为 1 次避免测试等待）"""
    import akshare as ak

    def _boom(date=None):
        raise ConnectionError("reset")

    monkeypatch.setattr(ak, "stock_zt_pool_em", _boom)
    monkeypatch.setattr(ak, "stock_zt_pool_zbgc_em", lambda date=None: _zb_df())
    monkeypatch.setattr("shared.utils.FETCH_RETRY", 1)
    monkeypatch.setattr("shared.utils.FETCH_RETRY_DELAY", 0)

    df = fetch_limit_pools("2026-07-31")
    assert len(df) == 1
    assert df.iloc[0]["pool_type"] == "broken"


# ---------- _merge_pool 纯函数 ----------

def test_merge_pool_sources_and_priority():
    leaders = pd.DataFrame([
        {"symbol": "000001", "name": "平安银行", "sector": "银行", "change_pct": 10.0},
        {"symbol": "000004", "name": "领涨股", "sector": "半导体", "change_pct": 7.0},
    ])
    pool = _merge_pool(_limit_rows(), leaders)

    assert len(pool) == 4
    first = pool.iloc[0]
    assert first["symbol"] == "000001"
    assert first["source"] == "连板+领涨"  # 连板优先，且与领涨合并标注
    assert first["lbc"] == 3

    sources = pool.set_index("symbol")["source"].to_dict()
    assert sources["000002"] == "涨停"
    assert sources["000003"] == "炸板"
    assert sources["000004"] == "领涨"


def test_merge_pool_cap(monkeypatch):
    monkeypatch.setattr("pipeline.hot_pool.HOT_POOL_MAX", 2)
    pool = _merge_pool(_limit_rows(), pd.DataFrame())
    assert len(pool) == 2
    assert pool.iloc[0]["source"] == "连板"  # 截断保留高优先级


def test_merge_pool_empty():
    assert _merge_pool(pd.DataFrame(), pd.DataFrame()).empty


# ---------- limit_pool / hot_pool 表读写 ----------

def test_limit_pool_and_hot_pool_roundtrip(tmp_path):
    db = tmp_path / "market.db"
    init_database(db)

    lp = _limit_rows()
    lp["trade_date"] = "2026-07-31"
    assert save_limit_pool(lp, db) == 3
    loaded = load_limit_pool(trade_date="2026-07-31", pool_type="up", db_path=db)
    assert len(loaded) == 2

    hp = pd.DataFrame([{
        "symbol": "000001", "name": "平安银行", "source": "连板",
        "sector": "银行", "change_pct": 10.0, "lbc": 3, "trade_date": "2026-07-31",
    }])
    assert save_hot_pool(hp, db) == 1
    loaded_hot = load_hot_pool(trade_date="2026-07-31", db_path=db)
    assert len(loaded_hot) == 1
    assert loaded_hot.iloc[0]["source"] == "连板"

    # REPLACE 幂等：同日同股重写不重复
    save_hot_pool(hp, db)
    assert len(load_hot_pool(trade_date="2026-07-31", db_path=db)) == 1


# ---------- signal_tracker 按系统分持有天数 ----------

def test_hot_signal_settles_at_5_days(tmp_path):
    """HOT-S 信号：第 5 个交易日强制到期结算（超短纪律）"""
    rows = _flat_rows("2026-01-02", 25)
    db = _make_db(tmp_path, {"000001": rows})
    record_signals(
        [{"symbol": "000001", "close": 100.0, "atr_20": 1.0, "period": 20}],
        system=HOT_SIGNAL_SYSTEM,
        signal_date="2026-01-01",
        db_path=db,
    )

    result = settle_signals(db_path=db, settle_date="2026-02-01")
    assert result["settled"] == 1
    detail = result["details"][0]
    assert detail["exit_reason"] == "到期"
    assert detail["exit_date"] == rows[4]["trade_date"]  # 第 5 根（count=5）


def test_s1a_signal_unaffected_by_holding_override(tmp_path):
    """S1-A 沿用全局 20 日持有上限，不受 HOT-S 覆盖影响"""
    rows = _flat_rows("2026-01-02", 25)
    db = _make_db(tmp_path, {"000001": rows})
    record_signals(
        [{"symbol": "000001", "close": 100.0, "atr_20": 1.0, "period": 20}],
        system="S1-A",
        signal_date="2026-01-01",
        db_path=db,
    )

    result = settle_signals(db_path=db, settle_date="2026-02-01")
    assert result["settled"] == 1
    assert result["details"][0]["exit_date"] == rows[19]["trade_date"]  # 第 20 根


def test_stats_grouped_by_system_includes_hot(tmp_path):
    """信号统计按系统分组，HOT-S 与 S1-A 各自独立可见"""
    rows = _flat_rows("2026-01-02", 25)
    db = _make_db(tmp_path, {"000001": rows, "000002": rows})
    record_signals(
        [{"symbol": "000001", "close": 100.0, "atr_20": 1.0, "period": 20}],
        system="S1-A", signal_date="2026-01-01", db_path=db,
    )
    record_signals(
        [{"symbol": "000002", "close": 100.0, "atr_20": 1.0, "period": 20}],
        system=HOT_SIGNAL_SYSTEM, signal_date="2026-01-01", db_path=db,
    )
    settle_signals(db_path=db, settle_date="2026-02-01")

    stats = signal_stats(db_path=db, days=365, as_of="2026-02-02")
    assert "S1-A" in stats["systems"]
    assert HOT_SIGNAL_SYSTEM in stats["systems"]
