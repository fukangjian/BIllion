"""
交易复盘配置 — 路径、合规规则（基于投资体系 V5.0）
"""
import os
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent
QUANT_ROOT = PROJECT_ROOT.parent
VAULT_ROOT = QUANT_ROOT.parent

# 数据存储
DATA_DIR = PROJECT_ROOT / "data"
TRADES_FILE = DATA_DIR / "trades.json"

# 报告输出目录
WEEKLY_OUTPUT_DIR = VAULT_ROOT / "【10】实盘记录" / "每周复盘"
MONTHLY_OUTPUT_DIR = VAULT_ROOT / "【10】实盘记录" / "每月复盘"
STATS_OUTPUT_DIR = VAULT_ROOT / "【10】实盘记录" / "统计"

# 账户权益（用于仓位比例计算，可通过环境变量覆盖）
ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "1000000"))

# 当前回撤状态（可通过环境变量或 CLI 设置）
# Normal / Caution / Defensive / Review
DRAWDOWN_STATE = os.getenv("DRAWDOWN_STATE", "Normal")

# --- 合规规则（投资体系 V5.0） ---

# 单笔风险绝对上限
MAX_SINGLE_RISK_PCT = 1.0  # 1%

# 各账户类型正常单笔风险上限 (%)
RISK_LIMITS_NORMAL = {
    "核心": 0.6,
    "核心复利": 0.6,
    "产业": 0.5,
    "产业趋势": 0.5,
    "创新药": 0.5,
    "事件": 0.35,
    "事件交易": 0.35,
    "实验": 0.5,
    "预埋": 0.15,
}

# 回撤期单笔风险上限 (%)
RISK_LIMITS_DRAWDOWN = {
    "核心": 0.3,
    "核心复利": 0.3,
    "产业": 0.25,
    "产业趋势": 0.25,
    "创新药": 0.25,
    "事件": 0.18,
    "事件交易": 0.18,
    "实验": 0.25,
    "预埋": 0.0,  # 暂停
}

# 单票仓位上限 (%)
POSITION_LIMITS = {
    "核心": 10.0,
    "核心复利": 10.0,
    "产业": 8.0,
    "产业趋势": 8.0,
    "创新药": 6.0,
    "创新药期权": 6.0,
    "事件": 4.0,
    "事件交易": 4.0,
    "事件驱动": 4.0,
    "实验": 1.0,
    "实验策略": 1.0,
}

# 风险簇上限 (% 市值暴露 / % 止损风险)
RISK_CLUSTER_LIMITS = {
    "创新药": {"exposure": 25.0, "stop_risk": 1.2},
    "AI算力": {"exposure": 25.0, "stop_risk": 1.2},
    "半导体": {"exposure": 20.0, "stop_risk": 1.0},
    "能源资源": {"exposure": 20.0, "stop_risk": 1.0},
    "商业航天": {"exposure": 15.0, "stop_risk": 0.8},
    "单一事件链": {"exposure": 15.0, "stop_risk": 0.7},
}

# 回撤状态下禁止的交易类型
FORBIDDEN_IN_DRAWDOWN = {
    "Caution": ["预埋"],
    "Defensive": ["预埋", "事件", "事件交易", "实验"],
    "Review": ["预埋", "事件", "事件交易", "实验", "产业", "产业趋势"],
}

# 有效的入场系统
VALID_ENTRY_SYSTEMS = ["S1-A", "S2-A", "预埋", "S1-A快速", "S2-A慢速"]

# AkShare 重试
FETCH_RETRY = 3
FETCH_RETRY_DELAY = 2
