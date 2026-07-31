"""
入场合规闸门与建仓链路测试 — 临时 trades.json / 临时扫描 JSON / 临时买入卡目录，不依赖网络
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import review.cli as cli
from config import ACCOUNT_TYPES, WATCHLIST
from position_calculator import calc_position, lookup_cluster
from review.buy_card import calc_dict_from_trade, generate_buy_card
from review.trade_log import Trade, TradeLog


# ---------- 测试工具 ----------

def _make_log(tmp_path, trades: list[dict] | None = None) -> TradeLog:
    """写临时 trades.json 并返回 TradeLog"""
    f = tmp_path / "trades.json"
    f.write_text(json.dumps(trades or [], ensure_ascii=False, indent=2), encoding="utf-8")
    return TradeLog(f)


def _patch_cli(tmp_path, monkeypatch):
    """把 cli 的 TradeLog 与买入卡输出目录重定向到 tmp_path"""
    log_file = tmp_path / "trades.json"
    log_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(cli, "TradeLog", lambda: TradeLog(log_file))
    card_dir = tmp_path / "cards"
    monkeypatch.setattr("review.buy_card.TRADE_LOG_OUTPUT_DIR", card_dir)
    return log_file, card_dir


def _add_args(**kw) -> SimpleNamespace:
    defaults = dict(
        symbol="600519", name="测试股", date="2026-07-29", account="核心",
        cluster="", system="S1-A", logic="", entry=100.0, stop=95.0,
        risk=0.5, shares=100, position=None, equity=1_000_000,
        off_system=False, force=False,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _scan_args(**kw) -> SimpleNamespace:
    defaults = dict(
        symbol="600519", date="2026-07-29", execute=True, system=None,
        account=None, cluster="", name="贵州茅台", equity=1_000_000, force=False,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _write_scan(tmp_path, monkeypatch, s1a: list[dict] | None = None, s2a: list[dict] | None = None,
                hot: list[dict] | None = None):
    """写临时扫描 JSON 并重定向 cli 的扫描目录"""
    scan_dir = tmp_path / "scans"
    scan_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "date": "2026-07-29",
        "market_state": "A",
        "breakout_s1a": s1a or [],
        "breakout_s2a": s2a or [],
        "hot_pool": {
            "available": bool(hot),
            "limit_up": [], "lianban": [], "broken": [],
            "hot_breakout": hot or [],
            "note": "",
        },
    }
    (scan_dir / "market_scan_2026-07-29.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(cli, "MARKET_SCAN_OUTPUT_DIR", scan_dir)


def _s2_item(**kw):
    d = {"symbol": "600519", "close": 100.0, "channel_high": 99.0,
         "breakout_pct": 1.01, "atr_20": 2.5, "period": 55}
    d.update(kw)
    return d


def _s1_item(**kw):
    d = {"symbol": "600519", "close": 100.0, "channel_high": 98.5,
         "breakout_pct": 1.52, "atr_20": 2.5, "period": 20}
    d.update(kw)
    return d


def _hot_item(**kw):
    """热点池突破候选（字段口径同 market_scanner._build_hot_section 的 hot_breakout）"""
    d = {"symbol": "600519", "close": 100.0, "channel_high": 98.0,
         "breakout_pct": 2.04, "atr_20": 2.5, "period": 20,
         "source": "连板", "sector": "食品饮料"}
    d.update(kw)
    return d


def _read_trades(log_file: Path) -> list[dict]:
    return json.loads(log_file.read_text(encoding="utf-8"))


# ---------- 仓位计算口径（与 config 一致） ----------

class TestCalcPosition:
    @pytest.mark.parametrize("account,expected", [
        ("核心", 1.0), ("产业", 1.0), ("事件", 1.0), ("实验", 0.5),
    ])
    def test_normal_rates_match_config(self, account, expected):
        calc = calc_position(1_000_000, 100, 95, account, "Normal")
        assert calc["风险率"] == expected

    @pytest.mark.parametrize("account,expected", [
        ("核心", 0.5), ("产业", 0.5), ("事件", 0.5), ("实验", 0.25),
    ])
    @pytest.mark.parametrize("state", ["Caution", "Defensive", "Review"])
    def test_drawdown_rates_match_config(self, account, state, expected):
        calc = calc_position(1_000_000, 100, 95, account, state)
        assert calc["风险率"] == expected

    def test_no_legacy_010_rate(self):
        """旧版英文口径的 0.1% 风险率不得再出现"""
        for account in ACCOUNT_TYPES:
            rate = calc_position(1_000_000, 100, 95, account, "Normal")["风险率"]
            assert rate != 0.1
            assert rate >= 0.15

    def test_shares_math_and_limit_reduction(self):
        """R=10000、每股风险 2 → 5000 股，仓位 50% 超核心 30% 上限 → 缩减至 3000 股"""
        calc = calc_position(1_000_000, 100, 98, "核心", "Normal")
        assert calc["风险预算"] == pytest.approx(10000.0)
        assert calc["每股风险"] == pytest.approx(2.0)
        assert calc["股数"] == 3000
        assert calc["仓位金额"] == pytest.approx(300000.0)
        assert calc["是否超限"] is True
        assert any("缩减" in n for n in calc["备注"])

    def test_no_reduction_within_limit(self):
        """事件 1.0% → R=10000、每股风险 10 → 1000 股，仓位 10% 未超 60% 上限"""
        calc = calc_position(1_000_000, 100, 90, "事件", "Normal")
        assert calc["股数"] == 1000
        assert calc["仓位比例"] == pytest.approx(10.0)
        assert calc["是否超限"] is False

    def test_zero_risk_rate_pauses(self):
        """回撤期风险率上限为 0 的类型（预埋）→ 0 股并备注暂停"""
        calc = calc_position(1_000_000, 100, 95, "预埋", "Caution")
        assert calc["股数"] == 0
        assert any("暂停" in n for n in calc["备注"])

    def test_add_prices_use_atr_when_given(self):
        calc = calc_position(1_000_000, 100, 95, "核心", "Normal", atr=4.0)
        assert calc["加仓价1"] == pytest.approx(102.0)
        assert calc["加仓价2"] == pytest.approx(104.0)

    def test_add_prices_fallback_per_share_risk(self):
        calc = calc_position(1_000_000, 100, 95, "核心", "Normal")
        assert calc["加仓价1"] == pytest.approx(102.5)
        assert calc["加仓价2"] == pytest.approx(105.0)

    def test_lookup_cluster(self):
        assert lookup_cluster("688235") == "创新药"
        assert lookup_cluster("600519") is None  # 已核对：白酒不在 V5.0 六簇内
        assert lookup_cluster("999999") is None


# ---------- cmd_add 合规闸门 ----------

class TestAddEntryGate:
    def test_high_violation_rejected(self, tmp_path, monkeypatch, capsys):
        """风险率 2.0% 超绝对上限 1.0%（高级）→ 拒绝写入"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        cli.cmd_add(_add_args(risk=2.0))
        out = capsys.readouterr().out
        assert "[拒绝]" in out
        assert "单笔风险超限" in out
        assert _read_trades(log_file) == []

    def test_force_writes_with_note(self, tmp_path, monkeypatch, capsys):
        """--force 强制写入，备注留痕"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        cli.cmd_add(_add_args(risk=2.0, force=True))
        out = capsys.readouterr().out
        assert "[OK] 已添加交易" in out
        trades = _read_trades(log_file)
        assert len(trades) == 1
        assert "⚠️ 强制建仓，违规：单笔风险超限" in trades[0]["备注"]

    def test_medium_violation_warns_but_writes(self, tmp_path, monkeypatch, capsys):
        """风险率 0.8% 超实验 0.5% 上限但未达绝对上限 1.0%（中级）→ 警告但写入"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        cli.cmd_add(_add_args(account="实验", risk=0.8))
        out = capsys.readouterr().out
        assert "[警告]" in out
        assert "[拒绝]" not in out
        assert len(_read_trades(log_file)) == 1

    def test_clean_add_writes_and_generates_card(self, tmp_path, monkeypatch, capsys):
        """合规交易正常写入并生成买入卡"""
        log_file, card_dir = _patch_cli(tmp_path, monkeypatch)
        cli.cmd_add(_add_args())
        out = capsys.readouterr().out
        assert "[OK] 已添加交易" in out
        trades = _read_trades(log_file)
        assert len(trades) == 1
        assert trades[0]["备注"] == ""
        cards = list(card_dir.glob("*_买入卡.md"))
        assert len(cards) == 1
        assert trades[0]["交易编号"] in cards[0].name
        assert "测试股" in cards[0].name

    def test_cluster_auto_lookup(self, tmp_path, monkeypatch):
        """未指定 --cluster 时自动查 INDUSTRY_MAP（688331 → 创新药）"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        cli.cmd_add(_add_args(symbol="688331"))
        assert _read_trades(log_file)[0]["风险簇"] == "创新药"

    def test_cluster_unknown_marked_unspecified(self, tmp_path, monkeypatch, capsys):
        """查不到映射 → 记为「未指定」并提示人工指定"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        cli.cmd_add(_add_args(symbol="999999"))
        out = capsys.readouterr().out
        assert "人工指定" in out
        assert _read_trades(log_file)[0]["风险簇"] == "未指定"

    def test_portfolio_cluster_violation_rejected(self, tmp_path, monkeypatch, capsys):
        """组合级：已有创新药簇持仓，再加一笔导致簇止损风险超限（高级）→ 拒绝"""
        existing = {
            "交易编号": "T_old", "日期": "2026-07-01", "股票代码": "688235",
            "股票名称": "旧仓", "账户类型": "产业", "风险簇": "创新药",
            "入场系统": "S2-A", "入场价": 100.0, "止损价": 90.0,
            "风险率": 1.0, "股数": 100, "仓位金额": 10000.0,
            "实际退出价": None, "退出日期": None, "是否系统内交易": True,
        }
        monkeypatch.setattr("review.entry_gate.derive_state_safe", lambda log=None: "Normal")
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        log_file.write_text(json.dumps([existing], ensure_ascii=False), encoding="utf-8")
        # 新笔同属创新药簇，簇止损风险合计 1.0 + 0.5 = 1.5 > 上限 1.0
        cli.cmd_add(_add_args(symbol="688331", cluster="创新药", risk=0.5))
        out = capsys.readouterr().out
        assert "风险簇止损风险超限" in out
        assert "[拒绝]" in out
        assert len(_read_trades(log_file)) == 1  # 仅原有持仓


# ---------- from-scan --execute ----------

class TestFromScanExecute:
    def test_execute_full_flow(self, tmp_path, monkeypatch, capsys):
        """S2 信号 → 核心账户 → 仓位计算 → 落库 → 买入卡"""
        log_file, card_dir = _patch_cli(tmp_path, monkeypatch)
        _write_scan(tmp_path, monkeypatch, s2a=[_s2_item()])
        cli.cmd_from_scan(_scan_args())
        out = capsys.readouterr().out
        assert "[OK] 已添加交易" in out
        trades = _read_trades(log_file)
        assert len(trades) == 1
        t = trades[0]
        assert t["入场系统"] == "S2-A"
        assert t["账户类型"] == "核心"
        assert t["入场价"] == pytest.approx(100.0)
        assert t["止损价"] == pytest.approx(95.0)  # 收盘 − 2×ATR
        assert t["风险率"] == pytest.approx(1.0)
        # R=10000 / 每股风险 5 = 2000 股 → 仓位 20% 未超核心 30% → 不缩减
        assert t["股数"] == 2000
        assert t["风险簇"] == "未指定"  # 600519 已核对不在 V5.0 六簇内
        assert t["是否系统内交易"] is True
        cards = list(card_dir.glob("*_买入卡.md"))
        assert len(cards) == 1
        content = cards[0].read_text(encoding="utf-8")
        assert "贵州茅台" in cards[0].name
        assert "**55 日最高价** | 99.0" in content or "**55 日最高价** | 99.00" in content
        assert "2.50" in content  # ATR
        assert "101.25" in content  # 加仓价1 = 100 + 0.5×2.5

    def test_dual_signal_defaults_s2(self, tmp_path, monkeypatch, capsys):
        """同股 S1+S2 双信号 → 默认按 S2（慢速）建仓并打印说明"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        _write_scan(tmp_path, monkeypatch,
                    s1a=[_s1_item()], s2a=[_s2_item(close=101.0, atr_20=3.0)])
        cli.cmd_from_scan(_scan_args())
        out = capsys.readouterr().out
        assert "默认按 S2-A" in out
        t = _read_trades(log_file)[0]
        assert t["入场系统"] == "S2-A"
        assert t["入场价"] == pytest.approx(101.0)
        assert t["止损价"] == pytest.approx(95.0)  # 101 − 2×3

    def test_dual_signal_system_override(self, tmp_path, monkeypatch):
        """--system S1-A 指定后按 S1 信号建仓"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        _write_scan(tmp_path, monkeypatch,
                    s1a=[_s1_item()], s2a=[_s2_item(close=101.0)])
        cli.cmd_from_scan(_scan_args(system="S1-A"))
        t = _read_trades(log_file)[0]
        assert t["入场系统"] == "S1-A"
        assert t["账户类型"] == "产业"  # S1 → 产业
        assert t["入场价"] == pytest.approx(100.0)

    def test_execute_high_violation_rejected_and_forced(self, tmp_path, monkeypatch, capsys):
        """Review 状态下 S1→产业 属回撤期禁止交易（高级）→ 拒绝；--force 强制写入并留痕"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        _write_scan(tmp_path, monkeypatch, s1a=[_s1_item()])
        monkeypatch.setattr(cli, "derive_state_safe", lambda log=None: "Review")

        cli.cmd_from_scan(_scan_args())
        out = capsys.readouterr().out
        assert "回撤期禁止交易" in out
        assert "[拒绝]" in out
        assert _read_trades(log_file) == []

        cli.cmd_from_scan(_scan_args(force=True))
        trades = _read_trades(log_file)
        assert len(trades) == 1
        assert "⚠️ 强制建仓，违规：回撤期禁止交易" in trades[0]["备注"]

    def test_execute_symbol_not_in_scan(self, tmp_path, monkeypatch, capsys):
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        _write_scan(tmp_path, monkeypatch, s2a=[_s2_item()])
        cli.cmd_from_scan(_scan_args(symbol="999999"))
        out = capsys.readouterr().out
        assert "[ERROR]" in out
        assert _read_trades(log_file) == []

    def test_print_only_unchanged_without_execute(self, tmp_path, monkeypatch, capsys):
        """不带 --execute 保持现状：只打印建议，不落库"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        _write_scan(tmp_path, monkeypatch, s2a=[_s2_item()])
        args = _scan_args(execute=False)
        cli.cmd_from_scan(args)
        out = capsys.readouterr().out
        assert "建议止损" in out
        assert _read_trades(log_file) == []


# ---------- from-scan 热点候选（HOT-S） ----------

class TestFromScanHot:
    def test_find_breakout_hot_returns_hot_s(self, tmp_path, monkeypatch):
        """hot_pool.hot_breakout 命中 → 返回 (记录, "HOT-S")"""
        _write_scan(tmp_path, monkeypatch, hot=[_hot_item()])
        scan = json.loads(
            (tmp_path / "scans" / "market_scan_2026-07-29.json").read_text(encoding="utf-8")
        )
        item, system = cli._find_breakout_in_scan(scan, "600519")
        assert system == "HOT-S"
        assert item["source"] == "连板"
        assert item["atr_20"] == pytest.approx(2.5)

    def test_execute_hot_defaults_account_event(self, tmp_path, monkeypatch, capsys):
        """仅热点信号 → 默认 HOT-S 建仓、账户映射「事件」、止损同为 ATR 口径"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        _write_scan(tmp_path, monkeypatch, hot=[_hot_item()])
        cli.cmd_from_scan(_scan_args())
        out = capsys.readouterr().out
        assert "[OK] 信号: HOT-S" in out
        t = _read_trades(log_file)[0]
        assert t["入场系统"] == "HOT-S"
        assert t["账户类型"] == "事件"  # HOT-S → 事件
        assert t["入场价"] == pytest.approx(100.0)
        assert t["止损价"] == pytest.approx(95.0)  # 收盘 − 2×ATR，与 S1/S2 同口径
        assert "热点来源 连板" in t["核心逻辑"]
        assert t["是否系统内交易"] is True

    def test_hot_and_s1_selected_by_system(self, tmp_path, monkeypatch, capsys):
        """S1-A 与热点信号并存：默认按 S1-A，--system HOT-S 时按热点候选建仓"""
        # 默认：走既有 S1/S2 链，热点仅提示
        _write_scan(tmp_path / "a", monkeypatch,
                    s1a=[_s1_item()], hot=[_hot_item(close=101.0)])
        log_file, _ = _patch_cli(tmp_path / "a", monkeypatch)
        cli.cmd_from_scan(_scan_args())
        out = capsys.readouterr().out
        assert "--system HOT-S" in out  # 提示另有热点信号
        t = _read_trades(log_file)[0]
        assert t["入场系统"] == "S1-A"
        assert t["账户类型"] == "产业"

        # --system HOT-S：按热点候选建仓（取热点记录的收盘价）
        _write_scan(tmp_path / "b", monkeypatch,
                    s1a=[_s1_item()], hot=[_hot_item(close=101.0)])
        log_file2, _ = _patch_cli(tmp_path / "b", monkeypatch)
        cli.cmd_from_scan(_scan_args(system="HOT-S"))
        t2 = _read_trades(log_file2)[0]
        assert t2["入场系统"] == "HOT-S"
        assert t2["账户类型"] == "事件"
        assert t2["入场价"] == pytest.approx(101.0)

    def test_print_only_hot_shows_stop_and_event_account(self, tmp_path, monkeypatch, capsys):
        """热点候选只打印建议：输出突破参数 + 建议止损，add 示例账户为事件"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        _write_scan(tmp_path, monkeypatch, hot=[_hot_item()])
        cli.cmd_from_scan(_scan_args(execute=False))
        out = capsys.readouterr().out
        assert "入场系统:   HOT-S" in out
        assert "建议止损:   95.00" in out
        assert "--account 事件 --system HOT-S" in out
        assert _read_trades(log_file) == []

    def test_not_in_any_list_error_shows_hot_candidates(self, tmp_path, monkeypatch, capsys):
        """不在任何候选列表 → 报错并列出 HOT-S 候选（只打印分支）"""
        log_file, _ = _patch_cli(tmp_path, monkeypatch)
        _write_scan(tmp_path, monkeypatch, hot=[_hot_item(symbol="605388")])
        cli.cmd_from_scan(_scan_args(symbol="999999", execute=False))
        out = capsys.readouterr().out
        assert "[ERROR]" in out
        assert "HOT-S 候选: 605388" in out
        assert _read_trades(log_file) == []


# ---------- 买入卡生成 ----------

class TestBuyCard:
    def _trade(self) -> Trade:
        return Trade(
            交易编号="T20260729_card01", 日期="2026-07-29", 股票代码="600519",
            股票名称="贵州茅台", 账户类型="核心", 风险簇="消费", 入场系统="S1-A",
            入场价=100.0, 止损价=95.0, 风险率=0.6, 股数=1000, 仓位金额=100000.0,
        )

    def test_generate_card_content(self, tmp_path):
        trade = self._trade()
        calc = calc_dict_from_trade(trade, 1_000_000)
        path = generate_buy_card(trade, calc, output_dir=tmp_path)
        assert path is not None and path.exists()
        assert path.name == "T20260729_card01_贵州茅台_买入卡.md"
        content = path.read_text(encoding="utf-8")
        assert "tags: [买入卡, 600519, S1-A]" in content
        assert "| **代码** | 600519 |" in content
        assert "| **账户类型** | 核心 |" in content
        assert "S1-A 20日突破" in content
        assert "| **初始止损** | 95.00 元 |" in content
        assert "102.5" in content  # 加仓价1 = 100 + 0.5×5
        assert "（待填写）" in content  # 投资逻辑/证据链等留待人工
        assert "- [ ] 已通过生存闸门（系统 0）" in content

    def test_generate_card_with_scan_info(self, tmp_path):
        trade = self._trade()
        calc = calc_dict_from_trade(trade, 1_000_000)
        scan = {"channel_high": 99.0, "atr_20": 2.5, "breakout_pct": 1.0, "period": 20}
        path = generate_buy_card(trade, calc, scan_info=scan, output_dir=tmp_path)
        content = path.read_text(encoding="utf-8")
        assert "**20 日最高价** | 99.0" in content
        assert "**ATR(20)** | 2.50" in content
        assert "价格 ≥ 99.0" in content
        assert "（入场价 − 2×ATR）" in content

    def test_card_failure_degrades(self, tmp_path, monkeypatch, capsys):
        """写入失败 → 打印警告返回 None，不抛异常"""
        def _boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr("review.buy_card.write_markdown", _boom)
        trade = self._trade()
        calc = calc_dict_from_trade(trade, 1_000_000)
        assert generate_buy_card(trade, calc, output_dir=tmp_path) is None
        assert "[警告]" in capsys.readouterr().out


# ---------- 持仓股并集补抓 ----------

class TestWatchlistUnion:
    def test_union_with_open_positions(self, tmp_path, monkeypatch):
        """WATCHLIST ∪ 未平仓持仓股，已平仓不计入"""
        import run_all

        open_trade = {
            "交易编号": "T_open", "日期": "2026-07-01", "股票代码": "601857",
            "股票名称": "中国石油", "账户类型": "核心", "入场系统": "S2-A",
            "入场价": 10.0, "止损价": 9.0, "风险率": 0.5, "股数": 1000,
            "仓位金额": 10000.0, "实际退出价": None, "退出日期": None,
        }
        closed_trade = dict(open_trade, 交易编号="T_closed", 股票代码="600000",
                            实际退出价=11.0, 退出日期="2026-07-20")
        log = _make_log(tmp_path, [open_trade, closed_trade])
        monkeypatch.setattr("review.positions.TradeLog", lambda *a, **k: log)
        symbols = run_all.watchlist_with_positions()
        assert symbols[: len(WATCHLIST)] == list(WATCHLIST)
        assert "601857" in symbols
        assert "600000" not in symbols
        assert len(symbols) == len(WATCHLIST) + 1

    def test_degrades_to_watchlist_on_failure(self, monkeypatch):
        """TradeLog 读取失败 → 降级为 WATCHLIST"""
        import run_all

        def _boom(*a, **k):
            raise RuntimeError("trades.json 损坏")
        monkeypatch.setattr("review.positions.get_position_for_watchlist", _boom)
        assert run_all.watchlist_with_positions() == list(WATCHLIST)
