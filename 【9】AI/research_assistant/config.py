"""
配置文件 — API 密钥、输出目录、模型选择、默认股票池
"""
import os
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent
AI_ROOT = PROJECT_ROOT.parent
VAULT_ROOT = AI_ROOT.parent

# 输出目录
OUTPUT_DIR = AI_ROOT / "daily_reports"
ANNOUNCEMENT_OUTPUT_DIR = AI_ROOT / "announcements"
FINANCIAL_OUTPUT_DIR = AI_ROOT / "financial_reports"
INDUSTRY_OUTPUT_DIR = AI_ROOT / "industry_maps"

# 量化模块路径（用于联动 market_scanner）
QUANT_ROOT = VAULT_ROOT / "【6】量化"
MARKET_SCANNER_OUTPUT = QUANT_ROOT / "data_pipeline" / "output"

# LLM 配置
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")  # openai 或 anthropic
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")

# LLM 重试
LLM_MAX_RETRIES = 3
LLM_RETRY_DELAY = 2  # 秒

# AkShare 重试
FETCH_RETRY = 3
FETCH_RETRY_DELAY = 2

# 默认关注股票（持仓 + 观察池，可通过环境变量或命令行覆盖）
DEFAULT_WATCHLIST = [
    "688192",  # 迪哲医药
    "600519",  # 贵州茅台
    "300750",  # 宁德时代
    "601012",  # 隆基绿能
    "300760",  # 迈瑞医疗
]

# 默认关注板块关键词
DEFAULT_SECTORS = ["创新药", "AI算力", "半导体", "新能源"]

# 公告获取数量上限
ANNOUNCEMENT_LIMIT = 10
