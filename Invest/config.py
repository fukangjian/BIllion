"""
统一配置文件 — 路径、API 密钥、扫描参数、合规规则
"""
import os
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent
VAULT_ROOT = PROJECT_ROOT.parent

# 数据目录
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "market.db"
TRADES_FILE = DATA_DIR / "trades.json"

# 输出目录（统一在 Invest/output/ 下）
OUTPUT_ROOT = PROJECT_ROOT / "output"
DAILY_REPORT_OUTPUT_DIR = OUTPUT_ROOT / "daily_reports"
ANNOUNCEMENT_OUTPUT_DIR = OUTPUT_ROOT / "announcements"
FINANCIAL_OUTPUT_DIR = OUTPUT_ROOT / "financial_reports"
INDUSTRY_OUTPUT_DIR = OUTPUT_ROOT / "industry_maps"
MARKET_SCAN_OUTPUT_DIR = OUTPUT_ROOT / "market_scans"
BACKTEST_OUTPUT_DIR = OUTPUT_ROOT / "backtest"

# 兼容别名（研究/管道模块沿用旧变量名）
OUTPUT_DIR = DAILY_REPORT_OUTPUT_DIR
MARKET_SCANNER_OUTPUT = MARKET_SCAN_OUTPUT_DIR
PIPELINE_OUTPUT_DIR = MARKET_SCAN_OUTPUT_DIR

# --- LLM 配置（Kimi / Moonshot API） ---
KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")
KIMI_BASE_URL = os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1")
KIMI_MODEL = os.getenv("KIMI_MODEL", "moonshot-v1-8k")

LLM_MAX_RETRIES = 3
LLM_RETRY_DELAY = 2  # 秒

# --- AkShare 重试 ---
FETCH_RETRY = 3
FETCH_RETRY_DELAY = 2  # 秒

DEFAULT_SECTORS = ["创新药", "AI算力", "半导体", "新能源"]
ANNOUNCEMENT_LIMIT = 10

# --- 数据管道：指数与通道参数 ---
DEFAULT_INDEX_SYMBOL = "000300"  # 沪深300
DEFAULT_INDEX_CODE = "sh000300"

CHANNEL_SHORT = 20
CHANNEL_LONG = 55
ATR_PERIOD = 20

MARKET_STATE = {
    "ma_weeks": 20,
    "volume_median_days": 60,
    "breadth_bull": 1.5,
    "breadth_bear": 0.7,
}

SECTOR_FETCH_LIMIT = 30

# 统一股票池（管道扫描 + 研究关注的基础列表）
WATCHLIST = [
    "000001",  # 平安银行
    "600519",  # 贵州茅台
    "000858",  # 五粮液
    "601318",  # 中国平安
    "300750",  # 宁德时代
    "002594",  # 比亚迪
    "600036",  # 招商银行
    "000333",  # 美的集团
    "601012",  # 隆基绿能
    "300760",  # 迈瑞医疗
]

# 研究模块默认关注列表（WATCHLIST 子集，侧重产业研究标的）
RESEARCH_WATCHLIST = [
    "600519",  # 贵州茅台
    "300750",  # 宁德时代
    "601012",  # 隆基绿能
    "300760",  # 迈瑞医疗
]

# --- 交易复盘：Obsidian 输出路径（保持写入 vault） ---
WEEKLY_OUTPUT_DIR = VAULT_ROOT / "【10】实盘记录" / "每周复盘"
MONTHLY_OUTPUT_DIR = VAULT_ROOT / "【10】实盘记录" / "每月复盘"
STATS_OUTPUT_DIR = VAULT_ROOT / "【10】实盘记录" / "统计"

ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "1000000"))
DRAWDOWN_STATE = os.getenv("DRAWDOWN_STATE", "Normal")

# --- 合规规则（投资体系 V5.0） ---
MAX_SINGLE_RISK_PCT = 1.0

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

RISK_LIMITS_DRAWDOWN = {
    "核心": 0.3,
    "核心复利": 0.3,
    "产业": 0.25,
    "产业趋势": 0.25,
    "创新药": 0.25,
    "事件": 0.18,
    "事件交易": 0.18,
    "实验": 0.25,
    "预埋": 0.0,
}

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

RISK_CLUSTER_LIMITS = {
    "创新药": {"exposure": 25.0, "stop_risk": 1.2},
    "AI算力": {"exposure": 25.0, "stop_risk": 1.2},
    "半导体": {"exposure": 20.0, "stop_risk": 1.0},
    "能源资源": {"exposure": 20.0, "stop_risk": 1.0},
    "商业航天": {"exposure": 15.0, "stop_risk": 0.8},
    "单一事件链": {"exposure": 15.0, "stop_risk": 0.7},
}

FORBIDDEN_IN_DRAWDOWN = {
    "Caution": ["预埋"],
    "Defensive": ["预埋", "事件", "事件交易", "实验"],
    "Review": ["预埋", "事件", "事件交易", "实验", "产业", "产业趋势"],
}

VALID_ENTRY_SYSTEMS = ["S1-A", "S2-A", "预埋", "S1-A快速", "S2-A慢速"]

# --- 数据获取并行度 ---
FETCH_MAX_WORKERS = 4  # 并行抓取线程数，避免被数据源限流

# --- 依赖说明（requirements.txt 中部分包为预留，当前代码未直接 import）---
# python-dotenv: 预留，用于未来 .env 自动加载 API 密钥
# requests: AkShare / openai 等库的间接依赖，显式声明便于版本锁定
# tabulate: 研究/管道模块 Markdown 表格输出的间接依赖

# --- 公告全文抓取与缓存 ---
ANNOUNCEMENT_CACHE_DIR = DATA_DIR / "announcements"
ANNOUNCEMENT_MAX_CHARS = 3000

# --- 多 LLM 提供商 ---
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

CUSTOM_LLM_API_KEY = os.getenv("CUSTOM_LLM_API_KEY", "")
CUSTOM_LLM_BASE_URL = os.getenv("CUSTOM_LLM_BASE_URL", "")
CUSTOM_LLM_MODEL = os.getenv("CUSTOM_LLM_MODEL", "")

LLM_CACHE_DIR = DATA_DIR / "llm_cache"
LLM_CACHE_TTL = 24 * 3600
LLM_PROVIDER_PRIORITY = ["kimi", "deepseek", "custom"]

# Kimi 长文模型（公告等 task_type=announcement 时使用）
KIMI_MODEL_LONG = os.getenv("KIMI_MODEL_LONG", "moonshot-v1-32k")

# --- FastAPI 服务与定时调度 ---
SCHEDULER_ENABLED = os.getenv("ENABLE_SCHEDULER", "").lower() == "true"
SCHEDULER_TIME = os.getenv("SCHEDULER_TIME", "08:30")
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8900"))
