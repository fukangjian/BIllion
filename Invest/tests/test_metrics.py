"""
review/metrics.py 单元测试 — 金额口径统计（无止损历史交易可用）与 R 口径共存
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review.metrics import compute_stats, stats_to_markdown
from review.trade_log import Trade


def _closed(entry, exit_price, shares=100, stop=0.0):
    return Trade(
        股票代码="600519",
        日期="2026-07-01",
        入场价=entry,
        止损价=stop,
        股数=shares,
        实际退出价=exit_price,
        退出日期="2026-07-02",
    )


class TestMoneyStats:
    def test_no_stop_trades_still_have_winrate_and_pnl(self):
        """无止损价的券商导入交易：胜率与总盈亏金额按金额口径统计"""
        trades = [
            _closed(10.0, 11.0, shares=100),   # +100
            _closed(10.0, 9.0, shares=100),    # -100
            _closed(10.0, 10.5, shares=200),   # +100
        ]
        stats = compute_stats(trades)
        assert stats.已平仓笔数 == 3
        assert stats.总盈亏金额 == 100.0
        assert stats.胜率 == 66.7
        # 无止损 → R 系指标保持 0
        assert stats.净R == 0.0
        assert stats.平均盈利R == 0.0

    def test_r_metrics_still_work_with_stops(self):
        """有止损价的交易：R 系指标照旧，胜率与 R 符号口径一致"""
        trades = [
            _closed(10.0, 12.0, shares=100, stop=9.0),   # +2R
            _closed(10.0, 9.0, shares=100, stop=9.0),    # -1R
        ]
        stats = compute_stats(trades)
        assert stats.胜率 == 50.0
        assert stats.平均盈利R == 2.0
        assert stats.平均亏损R == -1.0
        assert stats.净R == 1.0
        assert stats.总盈亏金额 == 100.0

    def test_markdown_contains_pnl_row(self):
        stats = compute_stats([_closed(10.0, 11.0, shares=100)])
        md = stats_to_markdown(stats)
        assert "总盈亏金额" in md
        assert "100.0 元" in md
