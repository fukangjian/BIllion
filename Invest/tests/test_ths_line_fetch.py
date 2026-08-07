"""
同花顺 v4/line 日线源测试 — mock requests，不触网
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import shared.data_fetcher as df_mod


def _js(payload_data: str, status: int = 200):
    class _Resp:
        status_code = status
        text = f'quotebridge_v4_line_hs_600519_01_2026({{"data":"{payload_data}"}})'
    return _Resp()


_TWO_DAYS = "20260105,1356.98,1403.86,1356.98,1397.98,7094942,10018086400.00,0.567,,,0;20260106,1404.53,1408.95,1388.51,1399.99,3958618,5649710200.00,0.316,,,0"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(df_mod.time, "sleep", lambda *_: None)


class TestThsLineFetch:
    def test_parse_and_columns(self, monkeypatch):
        monkeypatch.setattr("requests.get", lambda *a, **kw: _js(_TWO_DAYS))
        df = df_mod.fetch_stock_daily("600519", start_date="2026-01-01", end_date="2026-12-31")
        assert len(df) == 2
        row = df.iloc[0]
        assert row["trade_date"] == "2026-01-05"
        assert row["open"] == pytest.approx(1356.98)
        assert row["close"] == pytest.approx(1397.98)
        assert row["amount"] == pytest.approx(10018086400.0)
        assert list(df.columns) == ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]

    def test_failed_year_skipped(self, monkeypatch):
        """单年失败跳过，其他年份照常返回"""
        def fake_get(url, **kw):
            if "2025.js" in url:
                raise ConnectionError("reset")
            return _js(_TWO_DAYS)

        monkeypatch.setattr("requests.get", fake_get)
        df = df_mod.fetch_stock_daily("600519", start_date="2025-01-01", end_date="2026-12-31")
        assert len(df) == 2  # 只有 2026 年数据

    def test_all_fail_returns_empty(self, monkeypatch):
        def _boom(*a, **kw):
            raise ConnectionError("reset")

        monkeypatch.setattr("requests.get", _boom)
        assert df_mod.fetch_stock_daily("600519", start_date="2026-01-01").empty

    def test_date_range_filter(self, monkeypatch):
        monkeypatch.setattr("requests.get", lambda *a, **kw: _js(_TWO_DAYS))
        df = df_mod.fetch_stock_daily("600519", start_date="2026-01-06", end_date="2026-01-06")
        assert len(df) == 1
        assert df.iloc[0]["trade_date"] == "2026-01-06"

    def test_index_mapping(self, monkeypatch):
        """沪深300 映射 zs_1B0300；未映射代码降级空表"""
        monkeypatch.setattr("requests.get", lambda *a, **kw: _js(_TWO_DAYS))
        df = df_mod.fetch_index_daily("000300", start_date="2026-01-01", end_date="2026-12-31")
        assert len(df) == 2
        assert df_mod.fetch_index_daily("999999").empty
