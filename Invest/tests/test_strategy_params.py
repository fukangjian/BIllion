"""
策略参数收敛与策略枚举测试 — 断言各模块统一引用 config（防散落魔法数字）、
策略代码枚举生效、INDUSTRY_MAP 值对齐风险簇键；不依赖网络
"""
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import (
    ADD_SPACING_MAX,
    ADD_SPACING_MIN,
    ATR_PERIOD,
    ATR_STOP_MULT,
    BACKTEST_RISK_PCT,
    CHANNEL_LONG,
    CHANNEL_SHORT,
    EXIT_CHANNEL_PERIODS,
    INDUSTRY_MAP,
    LOT_SIZE,
    MAX_UNITS,
    RISK_CLUSTER_LIMITS,
    STRATEGY_CODES,
    STRATEGY_INFO,
    STRATEGY_PARAMS,
)


# ---------- 参数收敛：config 单一来源 ----------

class TestStrategyParamsConfig:
    """STRATEGY_PARAMS 为唯一来源，CHANNEL_SHORT/LONG 与 EXIT_CHANNEL_PERIODS 为派生别名"""

    def test_strategy_params_values(self):
        """数值锚定投资体系 V5.0 §4.3：S1-A 20入/10出，S2-A 55入/20出"""
        assert STRATEGY_PARAMS == {
            "S1-A": {"entry_channel": 20, "exit_channel": 10},
            "S2-A": {"entry_channel": 55, "exit_channel": 20},
        }
        assert ATR_PERIOD == 20
        assert ATR_STOP_MULT == 2.0
        assert ADD_SPACING_MIN == 0.5
        assert ADD_SPACING_MAX == 1.0
        assert MAX_UNITS == 3  # V5.0 §5.5 海龟三档：首仓 40% + 第2/3仓各 30%
        assert LOT_SIZE == 100

    def test_channel_aliases_derived_from_strategy_params(self):
        assert CHANNEL_SHORT == STRATEGY_PARAMS["S1-A"]["entry_channel"]
        assert CHANNEL_LONG == STRATEGY_PARAMS["S2-A"]["entry_channel"]

    def test_exit_channel_periods_derived_from_strategy_params(self):
        assert EXIT_CHANNEL_PERIODS["S1"] == STRATEGY_PARAMS["S1-A"]["exit_channel"]
        assert EXIT_CHANNEL_PERIODS["S2"] == STRATEGY_PARAMS["S2-A"]["exit_channel"]


class TestModuleDefaultsMatchConfig:
    """各消费模块默认参数必须与 config 一致"""

    def test_indicators_defaults(self):
        from pipeline import indicators

        assert (inspect.signature(indicators.calc_atr).parameters["period"].default
                == ATR_PERIOD)
        assert (inspect.signature(indicators.calc_donchian_channel).parameters["period"].default
                == STRATEGY_PARAMS["S1-A"]["entry_channel"])
        assert (inspect.signature(indicators.detect_breakout).parameters["period"].default
                == STRATEGY_PARAMS["S1-A"]["entry_channel"])
        assert (inspect.signature(indicators.scan_breakout_candidates).parameters["period"].default
                == STRATEGY_PARAMS["S1-A"]["entry_channel"])

    def test_market_scanner_uses_config_channels(self):
        from pipeline import market_scanner

        assert market_scanner.CHANNEL_SHORT == STRATEGY_PARAMS["S1-A"]["entry_channel"]
        assert market_scanner.CHANNEL_LONG == STRATEGY_PARAMS["S2-A"]["entry_channel"]

    def test_monitor_exit_channel_matches_config(self):
        from review.monitor import _exit_channel_period

        assert _exit_channel_period("S1-A") == STRATEGY_PARAMS["S1-A"]["exit_channel"]
        assert _exit_channel_period("S2-A") == STRATEGY_PARAMS["S2-A"]["exit_channel"]
        assert _exit_channel_period("STR-A") is None  # 非突破系统无退出通道，只查止损

    def test_signal_tracker_exit_channel_matches_config(self):
        from pipeline.signal_tracker import _exit_channel_period

        assert _exit_channel_period("S1-A") == STRATEGY_PARAMS["S1-A"]["exit_channel"]
        assert _exit_channel_period("S2-A") == STRATEGY_PARAMS["S2-A"]["exit_channel"]

    def test_signal_stop_uses_atr_stop_mult(self, tmp_path):
        """信号入库止损 = 收盘 − ATR_STOP_MULT × ATR（临时 SQLite）"""
        from pipeline.database import get_connection
        from pipeline.signal_tracker import record_signals

        db = tmp_path / "market.db"
        n = record_signals(
            [{"symbol": "600519", "close": 100.0, "atr_20": 2.5, "period": 20}],
            system="S1-A", signal_date="2026-07-29", db_path=db,
        )
        assert n == 1
        with get_connection(db) as conn:
            row = conn.execute("SELECT entry_price, stop_price FROM signals").fetchone()
        assert row[0] == pytest.approx(100.0)
        assert row[1] == pytest.approx(round(100.0 - 2.5 * ATR_STOP_MULT, 2))

    def test_backtest_strategy_params_match_config(self):
        from backtest.strategies import S1A_Strategy, S2A_Strategy

        s1 = dict(S1A_Strategy.params._getitems())
        s2 = dict(S2A_Strategy.params._getitems())
        assert s1["entry_period"] == STRATEGY_PARAMS["S1-A"]["entry_channel"]
        assert s1["exit_period"] == STRATEGY_PARAMS["S1-A"]["exit_channel"]
        assert s2["entry_period"] == STRATEGY_PARAMS["S2-A"]["entry_channel"]
        assert s2["exit_period"] == STRATEGY_PARAMS["S2-A"]["exit_channel"]
        for p in (s1, s2):
            assert p["atr_period"] == ATR_PERIOD
            assert p["atr_stop_mult"] == ATR_STOP_MULT
            assert p["add_spacing_min"] == ADD_SPACING_MIN
            assert p["add_spacing_max"] == ADD_SPACING_MAX
            assert p["max_units"] == MAX_UNITS
            assert p["risk_pct"] == BACKTEST_RISK_PCT


# ---------- 策略枚举（STRATEGY_CODES） ----------

class TestStrategyCodes:
    def test_codes_cover_six_strategies(self):
        """六策略清单（策略评估筛选框架 §1.1 五策略 + HOT-S 超短热点池），INFO 与 CODES 一一对应"""
        assert STRATEGY_CODES == ["S1-A", "S2-A", "STR-A", "STR-B", "STR-C", "HOT-S"]
        assert set(STRATEGY_INFO) == set(STRATEGY_CODES)
        for info in STRATEGY_INFO.values():
            assert info["名称"] and info["适用账户"] and info["典型持有期"]

    def test_add_rejects_invalid_system(self, monkeypatch):
        """cli add --system 非清单值被 argparse 拒绝（SystemExit）"""
        import review.cli as cli

        monkeypatch.setattr(sys, "argv", [
            "cli.py", "add", "600519", "--account", "核心", "--system", "预埋",
            "--entry", "100", "--stop", "95", "--risk", "0.5", "--shares", "100",
        ])
        with pytest.raises(SystemExit):
            cli.main()

    def test_add_accepts_listed_strategy_code(self, tmp_path, monkeypatch):
        """STR-A 等清单内代码可正常建仓落库"""
        import review.cli as cli
        from review.trade_log import TradeLog

        log_file = tmp_path / "trades.json"
        log_file.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(cli, "TradeLog", lambda: TradeLog(log_file))
        monkeypatch.setattr("review.buy_card.TRADE_LOG_OUTPUT_DIR", tmp_path / "cards")
        monkeypatch.setattr(sys, "argv", [
            "cli.py", "add", "600519", "--account", "核心", "--system", "STR-A",
            "--entry", "100", "--stop", "95", "--risk", "0.5", "--shares", "100",
        ])
        cli.main()
        trades = json.loads(log_file.read_text(encoding="utf-8"))
        assert len(trades) == 1
        assert trades[0]["入场系统"] == "STR-A"

    def test_free_text_system_flagged_off_system(self):
        """入场系统为自由文本（不在清单内）→ 非系统内交易(低)"""
        from review.compliance_check import check_single_trade
        from review.trade_log import Trade

        t = Trade(交易编号="T1", 股票代码="600519", 账户类型="核心", 入场系统="均线回踩",
                  入场价=100.0, 止损价=95.0, 风险率=0.5, 股数=100, 仓位金额=10000.0)
        kinds = [v.违规类型 for v in check_single_trade(t, "Normal", 1_000_000)]
        assert "非系统内交易" in kinds

    def test_listed_system_not_flagged(self):
        from review.compliance_check import check_single_trade
        from review.trade_log import Trade

        t = Trade(交易编号="T1", 股票代码="600519", 账户类型="核心", 入场系统="STR-A",
                  入场价=100.0, 止损价=95.0, 风险率=0.5, 股数=100, 仓位金额=10000.0)
        kinds = [v.违规类型 for v in check_single_trade(t, "Normal", 1_000_000)]
        assert "非系统内交易" not in kinds

    def test_off_system_flag_still_works(self):
        """--off-system 标记仍是低级违规"""
        from review.compliance_check import check_single_trade
        from review.trade_log import Trade

        t = Trade(交易编号="T1", 股票代码="600519", 账户类型="核心", 入场系统="S1-A",
                  入场价=100.0, 止损价=95.0, 风险率=0.5, 股数=100, 仓位金额=10000.0,
                  是否系统内交易=False)
        kinds = [v.违规类型 for v in check_single_trade(t, "Normal", 1_000_000)]
        assert "非系统内交易" in kinds


# ---------- 风险簇命名对齐 ----------

class TestRiskClusterMapping:
    def test_industry_map_values_align_with_cluster_limits(self):
        """INDUSTRY_MAP 的值只能是 RISK_CLUSTER_LIMITS 的键或 None（否则簇限额不生效）"""
        for symbol, cluster in INDUSTRY_MAP.items():
            assert cluster is None or cluster in RISK_CLUSTER_LIMITS, f"{symbol} → {cluster}"

    def test_lookup_cluster(self):
        from position_calculator import lookup_cluster

        assert lookup_cluster("688235") == "创新药"
        assert lookup_cluster("688331") == "创新药"
        assert lookup_cluster("600519") is None  # 已核对：白酒不在 V5.0 六簇内
        assert lookup_cluster("999999") is None

    def test_mapped_cluster_limit_enforced_at_portfolio_level(self):
        """映射后的创新药簇：两只持仓合计止损风险 1.6% > 上限 1.0% → 组合级违规生效"""
        from review.compliance_check import check_risk_cluster
        from review.trade_log import Trade

        trades = [
            Trade(交易编号="T1", 股票代码="688235", 账户类型="产业", 风险簇="创新药",
                  入场价=100.0, 止损价=90.0, 风险率=0.8, 股数=100, 仓位金额=10000.0),
            Trade(交易编号="T2", 股票代码="688331", 账户类型="产业", 风险簇="创新药",
                  入场价=100.0, 止损价=90.0, 风险率=0.8, 股数=100, 仓位金额=10000.0),
        ]
        violations = check_risk_cluster(trades, account_equity=1_000_000)
        assert any(v.违规类型 == "风险簇止损风险超限" for v in violations)
        assert all(v.交易编号 == "(组合)" for v in violations)

    def test_unmapped_stock_not_cluster_limited(self):
        """600519 映射为 None → 记「未指定」，不套用任何簇限额"""
        from review.compliance_check import check_risk_cluster
        from review.trade_log import Trade

        trades = [
            Trade(交易编号="T1", 股票代码="600519", 账户类型="核心", 风险簇="未指定",
                  入场价=100.0, 止损价=90.0, 风险率=2.0, 股数=100, 仓位金额=900000.0),
        ]
        assert check_risk_cluster(trades, account_equity=1_000_000) == []
