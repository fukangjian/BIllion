"""
合规规则边界测试 — 不依赖网络
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review.compliance_check import (
    check_risk_cluster,
    check_single_trade,
    run_compliance_check,
)
from review.trade_log import Trade


def _make_trade(**kwargs) -> Trade:
    defaults = {
        "交易编号": "T20260729_test01",
        "日期": "2026-07-29",
        "股票代码": "600519",
        "股票名称": "贵州茅台",
        "账户类型": "核心",
        "风险簇": "",
        "入场系统": "S1-A",
        "入场价": 1800.0,
        "止损价": 1700.0,
        "风险率": 0.5,
        "股数": 100,
        "仓位金额": 180000.0,
        "是否系统内交易": True,
    }
    defaults.update(kwargs)
    return Trade(**defaults)


class TestSingleTradeCompliance:
    def test_missing_stop_violation(self):
        trade = _make_trade(止损价=0)
        violations = check_single_trade(trade)
        types = [v.违规类型 for v in violations]
        assert "缺少止损" in types

    def test_risk_exceeds_absolute_max(self):
        trade = _make_trade(风险率=1.5)
        violations = check_single_trade(trade)
        assert any(v.违规类型 == "单笔风险超限" and v.严重程度 == "高" for v in violations)

    def test_risk_exceeds_drawdown_limit(self):
        # 核心 Caution 上限 0.5%（config.RISK_LIMITS_DRAWDOWN），0.8% 超限但未达绝对上限 1.0% → 中级
        trade = _make_trade(账户类型="核心", 风险率=0.8)
        violations = check_single_trade(trade, drawdown_state="Caution")
        assert any("Caution" in v.描述 for v in violations)

    def test_position_limit_exceeded(self):
        # 核心单票上限 30%（config.POSITION_LIMITS），40 万 / 100 万 = 40% 超限
        trade = _make_trade(账户类型="核心", 仓位金额=400000.0)
        violations = check_single_trade(trade, account_equity=1_000_000)
        assert any(v.违规类型 == "仓位超限" for v in violations)

    def test_forbidden_in_defensive_drawdown(self):
        trade = _make_trade(账户类型="事件", 入场系统="S1-A")
        violations = check_single_trade(trade, drawdown_state="Defensive")
        assert any(v.违规类型 == "回撤期禁止交易" for v in violations)

    def test_forbidden_in_review_drawdown_industry(self):
        trade = _make_trade(账户类型="产业趋势", 入场系统="S2-A")
        violations = check_single_trade(trade, drawdown_state="Review")
        assert any(v.违规类型 == "回撤期禁止交易" for v in violations)

    def test_off_system_trade_warning(self):
        trade = _make_trade(是否系统内交易=False)
        violations = check_single_trade(trade)
        assert any(v.违规类型 == "非系统内交易" for v in violations)


class TestRiskClusterCompliance:
    def test_cluster_exposure_exceeded(self):
        # 创新药簇暴露上限 60%（config.RISK_CLUSTER_LIMITS），70 万 / 100 万 = 70% 超限
        trades = [
            _make_trade(交易编号="T1", 风险簇="创新药", 仓位金额=400000, 风险率=0.5),
            _make_trade(交易编号="T2", 风险簇="创新药", 仓位金额=300000, 风险率=0.5),
        ]
        violations = check_risk_cluster(trades, account_equity=1_000_000)
        assert any(v.违规类型 == "风险簇暴露超限" for v in violations)

    def test_cluster_stop_risk_exceeded(self):
        trades = [
            _make_trade(交易编号="T1", 风险簇="AI算力", 仓位金额=100000, 风险率=0.8),
            _make_trade(交易编号="T2", 风险簇="AI算力", 仓位金额=100000, 风险率=0.6),
        ]
        violations = check_risk_cluster(trades, account_equity=1_000_000)
        assert any(v.违规类型 == "风险簇止损风险超限" for v in violations)

    def test_compliant_cluster_no_violation(self):
        trades = [
            _make_trade(交易编号="T1", 风险簇="半导体", 仓位金额=100000, 风险率=0.3),
        ]
        violations = check_risk_cluster(trades, account_equity=1_000_000)
        assert len(violations) == 0


class TestRunComplianceCheck:
    def test_empty_trades_100_percent_compliance(self):
        report = run_compliance_check(trades=[])
        assert report.合规率 == 100.0
        assert report.违规笔数 == 0

    def test_combined_check(self):
        trades = [_make_trade(风险率=2.0)]
        report = run_compliance_check(trades=trades)
        assert report.违规笔数 >= 1
