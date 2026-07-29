"""
持仓模块测试 — 使用临时 trades.json，不依赖网络
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review.positions import (
    export_open_positions,
    get_current_exposure,
    get_position_for_watchlist,
    get_total_risk,
    print_portfolio_summary,
)
from review.trade_log import Trade, TradeLog


@pytest.fixture
def temp_trade_log(tmp_path):
    """创建带测试数据的临时 TradeLog"""
    trades_file = tmp_path / "trades.json"
    trades = [
        {
            "交易编号": "T20260729_open01",
            "日期": "2026-07-29",
            "股票代码": "600519",
            "股票名称": "贵州茅台",
            "账户类型": "核心",
            "风险簇": "消费",
            "入场系统": "S1-A",
            "入场价": 1800.0,
            "止损价": 1700.0,
            "风险率": 0.5,
            "股数": 100,
            "仓位金额": 180000.0,
            "实际退出价": None,
            "退出日期": None,
            "是否系统内交易": True,
        },
        {
            "交易编号": "T20260729_open02",
            "日期": "2026-07-28",
            "股票代码": "300750",
            "股票名称": "宁德时代",
            "账户类型": "产业",
            "风险簇": "新能源",
            "入场系统": "S2-A",
            "入场价": 200.0,
            "止损价": 185.0,
            "风险率": 0.4,
            "股数": 500,
            "仓位金额": 100000.0,
            "实际退出价": None,
            "退出日期": None,
            "是否系统内交易": True,
        },
        {
            "交易编号": "T20260720_closed01",
            "日期": "2026-07-20",
            "股票代码": "601012",
            "股票名称": "隆基绿能",
            "账户类型": "产业",
            "风险簇": "新能源",
            "入场系统": "S1-A",
            "入场价": 25.0,
            "止损价": 23.0,
            "风险率": 0.3,
            "股数": 1000,
            "仓位金额": 25000.0,
            "实际退出价": 28.0,
            "退出日期": "2026-07-25",
            "是否系统内交易": True,
        },
    ]
    trades_file.write_text(json.dumps(trades, ensure_ascii=False, indent=2), encoding="utf-8")
    return TradeLog(trades_file)


class TestExportOpenPositions:
    def test_returns_only_open_trades(self, temp_trade_log):
        positions = export_open_positions(temp_trade_log)
        assert len(positions) == 2
        symbols = {p["股票代码"] for p in positions}
        assert symbols == {"600519", "300750"}

    def test_returns_dict_format(self, temp_trade_log):
        positions = export_open_positions(temp_trade_log)
        assert all(isinstance(p, dict) for p in positions)
        assert "入场价" in positions[0]


class TestGetCurrentExposure:
    def test_exposure_for_industry(self, temp_trade_log):
        exposure = get_current_exposure("新能源", temp_trade_log, account_equity=1_000_000)
        assert exposure == pytest.approx(10.0)  # 100000 / 1e6 * 100

    def test_exposure_zero_for_unknown(self, temp_trade_log):
        exposure = get_current_exposure("创新药", temp_trade_log, account_equity=1_000_000)
        assert exposure == 0.0


class TestGetTotalRisk:
    def test_total_risk_open_only(self, temp_trade_log):
        total = get_total_risk(temp_trade_log)
        assert total == pytest.approx(0.9)  # 0.5 + 0.4


class TestGetPositionForWatchlist:
    def test_returns_unique_symbols(self, temp_trade_log):
        symbols = get_position_for_watchlist(temp_trade_log)
        assert symbols == ["600519", "300750"]


class TestPrintPortfolioSummary:
    def test_print_no_crash(self, temp_trade_log, capsys):
        print_portfolio_summary(temp_trade_log, account_equity=1_000_000)
        captured = capsys.readouterr()
        assert "600519" in captured.out
        assert "合计风险敞口" in captured.out

    def test_print_empty(self, tmp_path, capsys):
        empty_log = TradeLog(tmp_path / "empty.json")
        print_portfolio_summary(empty_log)
        captured = capsys.readouterr()
        assert "暂无持仓" in captured.out
