"""
组合热度与市场状态门禁测试 — 纯函数 + 临时 DB，不依赖网络（V5.0 §5.6/§1.2）
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.database import get_connection, init_database
from review.compliance_check import check_market_conditions
from review.entry_gate import check_entry, get_latest_market_state
from review.trade_log import Trade, TradeLog


def _trade(risk: float = 0.5, system: str = "S1-A", account: str = "产业",
           symbol: str = "600519", tid: str = "T1") -> Trade:
    return Trade(
        交易编号=tid, 股票代码=symbol, 账户类型=account, 入场系统=system,
        入场价=100.0, 止损价=95.0, 风险率=risk, 股数=100, 仓位金额=10000.0,
    )


def _kinds(violations) -> list[str]:
    return [v.违规类型 for v in violations]


class TestPortfolioHeat:
    def test_under_limit_no_violation(self):
        """B 状态上限 3%：存量 1.0% + 本笔 0.5% = 1.5% → 无违规"""
        v = check_market_conditions(_trade(0.5), [_trade(1.0, tid="T0")], "B")
        assert v == []

    def test_over_limit_high_violation(self):
        """B 状态：存量 2.8% + 本笔 0.5% = 3.3% > 3% → 高级「账户热度超限」"""
        v = check_market_conditions(_trade(0.5), [_trade(2.8, tid="T0")], "B")
        assert len(v) == 1
        assert v[0].违规类型 == "账户热度超限"
        assert v[0].严重程度 == "高"
        assert v[0].交易编号 == "(组合)"

    def test_state_a_allows_higher_heat(self):
        """A 强趋势上限 4%：同样的 3.3% 不超限"""
        v = check_market_conditions(_trade(0.5), [_trade(2.8, tid="T0")], "A")
        assert v == []

    def test_state_c_tighter_heat(self):
        """C 状态上限 1.5%：存量 1.2% + 本笔 0.5% = 1.7% → 超限 + 震荡市提示"""
        v = check_market_conditions(_trade(0.5), [_trade(1.2, tid="T0")], "C")
        kinds = _kinds(v)
        assert "账户热度超限" in kinds
        assert "市场状态提示" in kinds  # C + S1-A 中级警告

    def test_state_d_blocks_everything(self):
        """D 状态上限 0：任何风险率 > 0 的新仓 → 热度超限（高）；趋势系统再加禁止建仓（高）"""
        v = check_market_conditions(_trade(0.5, system="S1-A"), [], "D")
        high = {x.违规类型 for x in v if x.严重程度 == "高"}
        assert "账户热度超限" in high
        assert "市场状态禁止建仓" in high

    def test_state_d_hot_system_only_heat(self):
        """D 状态 + HOT-S（非趋势系统）→ 只有热度超限，无「市场状态禁止建仓」"""
        v = check_market_conditions(_trade(0.3, system="HOT-S", account="事件"), [], "D")
        kinds = _kinds(v)
        assert "账户热度超限" in kinds
        assert "市场状态禁止建仓" not in kinds

    def test_none_state_skips(self):
        """无市场状态数据（降级）→ 跳过检查"""
        assert check_market_conditions(_trade(0.5), [_trade(2.8, tid="T0")], None) == []

    def test_unknown_state_skips(self):
        assert check_market_conditions(_trade(0.5), [], "X") == []


class TestGetLatestMarketState:
    def test_reads_latest(self, tmp_path):
        """取 market_state 表最新一日的状态"""
        db = tmp_path / "market.db"
        init_database(db)
        with get_connection(db) as conn:
            conn.execute("INSERT INTO market_state (trade_date, state) VALUES ('2026-08-01', 'B')")
            conn.execute("INSERT INTO market_state (trade_date, state) VALUES ('2026-08-05', 'D')")
        assert get_latest_market_state(db) == "D"

    def test_empty_table_returns_none(self, tmp_path):
        db = tmp_path / "market.db"
        init_database(db)
        assert get_latest_market_state(db) is None

    def test_missing_db_returns_none(self, tmp_path):
        """读取失败降级 None（不抛异常）"""
        assert get_latest_market_state(tmp_path / "nonexistent" / "x.db") is None


class TestCheckEntryMarketGate:
    def test_d_state_trend_entry_high_violation(self, tmp_path):
        """check_entry 注入 D 状态 → S1-A 建仓出现高级违规（禁止建仓 + 热度超限）"""
        log_file = tmp_path / "trades.json"
        log_file.write_text("[]", encoding="utf-8")
        violations, _ = check_entry(
            _trade(0.5, system="S1-A"),
            trade_log=TradeLog(log_file),
            account_equity=1_000_000,
            drawdown_state="Normal",
            market_state="D",
        )
        high = {v.违规类型 for v in violations if v.严重程度 == "高"}
        assert "市场状态禁止建仓" in high
        assert "账户热度超限" in high

    def test_b_state_clean_entry_passes(self, tmp_path):
        """B 状态 + 合规小仓 → 无高级违规"""
        log_file = tmp_path / "trades.json"
        log_file.write_text("[]", encoding="utf-8")
        violations, _ = check_entry(
            _trade(0.5, system="S1-A"),
            trade_log=TradeLog(log_file),
            account_equity=1_000_000,
            drawdown_state="Normal",
            market_state="B",
        )
        assert [v for v in violations if v.严重程度 == "高"] == []

    def test_market_state_from_db(self, tmp_path):
        """不显式传 market_state 时从 db_path 读取（D 状态生效）"""
        db = tmp_path / "market.db"
        init_database(db)
        with get_connection(db) as conn:
            conn.execute("INSERT INTO market_state (trade_date, state) VALUES ('2026-08-05', 'D')")
        log_file = tmp_path / "trades.json"
        log_file.write_text("[]", encoding="utf-8")
        violations, _ = check_entry(
            _trade(0.5, system="S2-A"),
            trade_log=TradeLog(log_file),
            account_equity=1_000_000,
            drawdown_state="Normal",
            db_path=db,
        )
        assert "市场状态禁止建仓" in _kinds(violations)


class TestLayeredSignalStats:
    def test_by_state_grouping(self, tmp_path):
        """信号分层统计：同一系统在不同市场状态下的信号分组正确；无状态归入「未知」"""
        from pipeline.signal_tracker import signal_stats

        db = tmp_path / "market.db"
        init_database(db)
        rows = [
            ("2026-07-01", "600519", "S1-A", 2.0, "2026-07-10", "A"),
            ("2026-07-02", "600519", "S1-A", -1.0, "2026-07-12", "D"),
            ("2026-07-03", "600519", "S1-A", 1.0, "2026-07-14", None),
        ]
        with get_connection(db) as conn:
            for sd, sym, sys_, r, ed, ms in rows:
                conn.execute(
                    "INSERT INTO signals (signal_date, symbol, system, entry_price, stop_price, "
                    "status, exit_date, exit_price, exit_reason, r_multiple, market_state, created_at) "
                    "VALUES (?, ?, ?, 100, 96, 'closed', ?, ?, '测试', ?, ?, '2026-07-01 09:00:00')",
                    (sd, sym, sys_, ed, 100 + r * 4, r, ms),
                )
        stats = signal_stats(db_path=db, days=90, as_of="2026-08-01")
        by_state = stats["by_state"]
        assert set(by_state) == {"A", "D", "未知"}
        assert by_state["A"]["S1-A"]["avg_r"] == pytest.approx(2.0)
        assert by_state["D"]["S1-A"]["avg_r"] == pytest.approx(-1.0)
        assert by_state["未知"]["S1-A"]["closed"] == 1

    def test_layered_markdown_rendered(self, tmp_path):
        """分层表写入 Markdown（周报/月报信号验证节共用）"""
        from pipeline.signal_tracker import signal_stats, signal_stats_to_markdown

        db = tmp_path / "market.db"
        init_database(db)
        with get_connection(db) as conn:
            conn.execute(
                "INSERT INTO signals (signal_date, symbol, system, entry_price, stop_price, "
                "status, exit_date, exit_price, exit_reason, r_multiple, market_state, created_at) "
                "VALUES ('2026-07-01', '600519', 'S1-A', 100, 96, 'closed', "
                "'2026-07-10', 108, '通道退出', 2.0, 'B', '2026-07-01 09:00:00')"
            )
        md = signal_stats_to_markdown(signal_stats(db_path=db, days=90, as_of="2026-08-01"))
        assert "按市场状态分层" in md
        assert "| B | S1-A |" in md
