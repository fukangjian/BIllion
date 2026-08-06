"""
Web 控制台 API 接线测试 — FastAPI TestClient，写操作全部 monkeypatch 到 mock，不碰真实数据
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from server import app

    return TestClient(app)


class TestPages:
    def test_root_serves_console(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Invest 控制台" in r.text

    def test_api_index(self, client):
        r = client.get("/api")
        assert r.status_code == 200
        assert any("from-scan" in e for e in r.json()["endpoints"])


class TestQueryEndpoints:
    def test_overview_shape(self, client, monkeypatch):
        monkeypatch.setattr("review.trade_ops.get_overview",
                            lambda: {"equity": 32500, "open_count": 0, "market_state": "C"})
        r = client.get("/api/overview")
        assert r.status_code == 200
        assert r.json()["market_state"] == "C"

    def test_plan_shape(self, client, monkeypatch):
        monkeypatch.setattr("review.trade_ops.get_daily_plan",
                            lambda: {"available": True, "buy_candidates": [], "position_actions": []})
        r = client.get("/api/plan")
        assert r.status_code == 200
        assert r.json()["available"] is True

    def test_sell_check(self, client, monkeypatch):
        monkeypatch.setattr("review.trade_ops.get_sell_check_lines",
                            lambda symbol: {"found": False, "lines": ["无持仓"]})
        r = client.get("/api/sell-check/600519")
        assert r.status_code == 200
        assert r.json()["lines"] == ["无持仓"]


class TestWriteEndpoints:
    def test_from_scan_conflict_returns_409(self, client, monkeypatch):
        """闸门拒绝 → 409 + 违规明细"""
        monkeypatch.setattr("review.trade_ops.execute_from_scan", lambda **kw: {
            "ok": False, "reason": "高级违规拒绝", "high": ["禁买板块: 命中 300 前缀"], "others": [],
        })
        r = client.post("/api/trades/from-scan", json={"symbol": "300750"})
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "禁买板块" in detail["high"][0]

    def test_from_scan_ok(self, client, monkeypatch):
        monkeypatch.setattr("review.trade_ops.execute_from_scan", lambda **kw: {
            "ok": True, "交易编号": "T1", "system": "S1-A", "calc": {"股数": 100},
        })
        r = client.post("/api/trades/from-scan", json={"symbol": "600519"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_sell_ok_and_conflict(self, client, monkeypatch):
        monkeypatch.setattr("review.trade_ops.execute_sell", lambda **kw: {
            "ok": True, "mode": "全平", "交易编号": "T1", "R倍数": 2.0,
        })
        assert client.post("/api/trades/sell",
                           json={"symbol": "600519", "shares": 100, "price": 108}).status_code == 200

        monkeypatch.setattr("review.trade_ops.execute_sell", lambda **kw: {
            "ok": False, "reason": "当前无持仓中交易",
        })
        r = client.post("/api/trades/sell", json={"symbol": "600519", "shares": 100, "price": 108})
        assert r.status_code == 409

    def test_add_position_rejected(self, client, monkeypatch):
        monkeypatch.setattr("review.trade_ops.execute_add_position", lambda **kw: {
            "ok": False, "reason": "未达加仓间距（触发价 101.00，还差 0.50）",
        })
        r = client.post("/api/trades/add-position", json={"symbol": "600519"})
        assert r.status_code == 409
        assert "未达加仓间距" in r.json()["detail"]["reason"]

    def test_update_stop(self, client, monkeypatch):
        monkeypatch.setattr("review.trade_ops.execute_update_stop", lambda **kw: {
            "ok": True, "交易编号": "T1", "旧止损": 0, "新止损": 5.5,
        })
        r = client.post("/api/trades/update-stop", json={"trade_id": "T1", "stop": 5.5})
        assert r.status_code == 200
