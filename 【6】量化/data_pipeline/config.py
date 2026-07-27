"""
配置文件 — 数据库路径、数据源设置、扫描参数
"""
from pathlib import Path

# 项目根目录（【6】量化）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 数据管道目录
PIPELINE_ROOT = Path(__file__).resolve().parent

# SQLite 数据库路径
DATA_DIR = PIPELINE_ROOT / "data"
DB_PATH = DATA_DIR / "market.db"

# 输出目录（Markdown 扫描报告）
OUTPUT_DIR = PIPELINE_ROOT / "output"

# 默认股票池（沪深300成分 + 常用指数）
DEFAULT_INDEX_SYMBOL = "000300"  # 沪深300
DEFAULT_INDEX_CODE = "sh000300"

# 通道周期
CHANNEL_SHORT = 20
CHANNEL_LONG = 55
ATR_PERIOD = 20

# 市场状态判断参数
MARKET_STATE = {
    "ma_weeks": 20,           # 20周均线
    "volume_median_days": 60, # 成交额中位数计算天数
    "breadth_bull": 1.5,      # 上涨/下跌比 > 1.5 偏多
    "breadth_bear": 0.7,      # 上涨/下跌比 < 0.7 偏空
}

# AkShare 请求重试
FETCH_RETRY = 3
FETCH_RETRY_DELAY = 2  # 秒

# 默认扫描股票列表（可从数据库或配置文件扩展）
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

# 板块列表（东方财富行业板块，运行时动态获取）
SECTOR_FETCH_LIMIT = 30
