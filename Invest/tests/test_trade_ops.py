"""
程序化交易操作层测试 — 临时 trades.json/临时 DB/mock 行情与扫描 JSON，不依赖网络、不碰真实数据
"""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import review.trade_ops as ops
from review.trade_log import TradeLog


@pytest.fixture(autouse=True)
def _pin_market_state(monkeypatch):
    """固定市场状态为 A（闸门市场状态检查不依赖真实 market.db）"""
    monkeypatch.setattr("review.entry_gate.get_latest_market_state", lambda db_path=None: "A")


@pytest.fixture(autouse=True)
def _isolate_cards(tmp_path, monkeypatch):
    """买入卡输出重定向到 tmp（不污染 vault）"""
    monkeypatch.setattr("review.buy_card.TRADE_LOG_OUTPUT_DIR", tmp_path / "cards")


def _log(tmp_path, trades=None) -> TradeLog:
    f = tmp_path / "trades.json"
    f.write_text(json.dumps(trades or [], ensure_ascii=False), encoding="utf-8")
    return TradeLog(f)


def _open_trade(**kw) -> dict:
    d = {
        "交易编号": "T001", "日期": "2026-08-01", "股票代码": "600519", "股票名称": "测试股",
        "账户类型": "产业", "风险簇": "", "入场系统": "S1-A",
        "入场价": 100.0, "止损价": 96.0, "风险率": 0.5, "股数": 1000, "仓位金额": 100000.0,
        "实际退出价": None, "退出日期": None, "是否系统内交易": True,
    }
    d.update(kw)
    return d


class TestExecuteAdd:
    def test_clean_add_ok(self, tmp_path):
        log = _log(tmp_path)
        r = ops.execute_add("600519", "核心", "S1-A", 100.0, 95.0, 0.5, 100,
                            equity=1_000_000, trade_log=log)
        assert r["ok"] is True
        assert r["交易编号"]
        assert len(log.list_all()) == 1

    def test_high_violation_rejected(self, tmp_path):
        log = _log(tmp_path)
        r = ops.execute_add("600519", "核心", "S1-A", 100.0, 95.0, 2.0, 100,
                            equity=1_000_000, trade_log=log)
        assert r["ok"] is False
        assert any("单笔风险超限" in h for h in r["high"])
        assert log.list_all() == []

    def test_force_writes_with_note(self, tmp_path):
        log = _log(tmp_path)
        r = ops.execute_add("600519", "核心", "S1-A", 100.0, 95.0, 2.0, 100,
                            force=True, equity=1_000_000, trade_log=log)
        assert r["ok"] is True
        assert "强制建仓" in log.list_all()[0].备注


class TestExecuteFromScan:
    def _scan(self):
        return {
            "date": "2026-08-06", "market_state": "A",
            "breakout_s1a": [{"symbol": "600519", "close": 100.0, "channel_high": 98.0,
                              "breakout_pct": 2.0, "atr_20": 2.0, "period": 20}],
            "breakout_s2a": [], "hot_pool": {},
        }

    def test_execute_ok(self, tmp_path):
        log = _log(tmp_path)
        r = ops.execute_from_scan("600519", equity=100_000, trade_log=log, scan_data=self._scan())
        assert r["ok"] is True
        t = log.list_all()[0]
        assert t.入场系统 == "S1-A"
        assert t.止损价 == 96.0

    def test_symbol_not_in_scan(self, tmp_path):
        log = _log(tmp_path)
        r = ops.execute_from_scan("000858", equity=100_000, trade_log=log, scan_data=self._scan())
        assert r["ok"] is False
        assert "不在最新突破候选" in r["reason"]


class TestExecuteSell:
    def test_full_sell(self, tmp_path):
        log = _log(tmp_path, [_open_trade()])
        r = ops.execute_sell("600519", shares=1000, price=108.0, trade_log=log)
        assert r["ok"] is True and r["mode"] == "全平"
        assert r["R倍数"] == pytest.approx(2.0)

    def test_partial_sell_split(self, tmp_path):
        log = _log(tmp_path, [_open_trade()])
        r = ops.execute_sell("600519", shares=400, price=108.0, trade_log=log)
        assert r["ok"] is True and r["mode"] == "部分卖出"
        trades = log.list_all()
        assert len(trades) == 2
        open_t = [t for t in trades if not t.is_closed][0]
        assert open_t.股数 == 600

    def test_sell_no_position(self, tmp_path):
        log = _log(tmp_path)
        r = ops.execute_sell("600519", shares=100, price=108.0, trade_log=log)
        assert r["ok"] is False


class TestUpdateStop:
    def test_update_ok(self, tmp_path):
        log = _log(tmp_path, [_open_trade()])
        r = ops.execute_update_stop("T001", 98.0, trade_log=log)
        assert r["ok"] is True
        assert log.get("T001").止损价 == 98.0
        assert "止损调整" in log.get("T001").备注

    def test_update_closed_rejected(self, tmp_path):
        t = _open_trade(实际退出价=108.0, 退出日期="2026-08-03")
        log = _log(tmp_path, [t])
        r = ops.execute_update_stop("T001", 98.0, trade_log=log)
        assert r["ok"] is False


class TestQueries:
    def test_positions_view(self, tmp_path):
        from pipeline.database import init_database

        db = tmp_path / "market.db"
        init_database(db)
        log = _log(tmp_path, [_open_trade()])
        r = ops.get_positions_view(trade_log=log, db_path=db, equity=100_000)
        assert len(r["positions"]) == 1
        assert r["positions"][0]["股票代码"] == "600519"
        assert r["total_risk_pct"] == 0.5

    def test_overview_empty_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ops, "MARKET_SCAN_OUTPUT_DIR", tmp_path / "no_scan")
        log = _log(tmp_path)
        r = ops.get_overview(trade_log=log, db_path=tmp_path / "empty.db")
        assert r["open_count"] == 0
        assert r["drawdown_state"] == "Normal"


class TestVersion:
    def test_overview_includes_version(self, tmp_path, monkeypatch):
        """概览携带系统版本号（UI 顶栏/使用说明展示）"""
        from config import APP_VERSION

        monkeypatch.setattr(ops, "MARKET_SCAN_OUTPUT_DIR", tmp_path / "no_scan")
        log = _log(tmp_path)
        r = ops.get_overview(trade_log=log, db_path=tmp_path / "empty.db")
        assert r["version"] == APP_VERSION
