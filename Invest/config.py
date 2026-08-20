"""
统一配置文件 — 路径、API 密钥、扫描参数、合规规则
"""
import os
from pathlib import Path

# 系统版本（UI 顶栏与使用说明弹窗展示）
APP_VERSION = "v2026.08"

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
KIMI_MODEL = os.getenv("KIMI_MODEL", "kimi-k2.6")  # moonshot-v1 系列 2026-08-31 停服；默认 kimi-k2.6（兼顾能力与费用）

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

# --- 突破策略参数（投资体系 V5.0 §4.3 海龟突破系统，全仓库单一来源） ---
# S1-A 快速系统：20 日通道突破入场 / 10 日通道低点退出
# S2-A 慢速系统：55 日通道突破入场 / 20 日通道低点退出
STRATEGY_PARAMS = {
    "S1-A": {"entry_channel": 20, "exit_channel": 10},
    "S2-A": {"entry_channel": 55, "exit_channel": 20},
}

ATR_PERIOD = 20            # N = ATR(20)，Wilder 平滑
ATR_STOP_MULT = 2.0        # 初始止损 = 入场价 − 2N
ADD_SPACING_MIN = 0.5      # 加仓最小间距 0.5N（每涨 0.5N 加一单位）
ADD_SPACING_MAX = 1.0      # 加仓间距上限 1N（单根跳空超 1N 不追，跳过的单位不补）
MAX_UNITS = 3              # 最大单位数（V5.0 §5.5 海龟三档：首仓 40% + 第2/3仓各 30%，即首仓 + 最多 2 次加仓）
LOT_SIZE = 100             # A 股整手股数
BACKTEST_RISK_PCT = 0.005  # 回测每单位风险比例 0.5%（实盘风险率由 RISK_LIMITS 账户限额决定）

# 兼容别名：扫描默认通道周期（值统一来自 STRATEGY_PARAMS，勿再单独定义数值）
CHANNEL_SHORT = STRATEGY_PARAMS["S1-A"]["entry_channel"]
CHANNEL_LONG = STRATEGY_PARAMS["S2-A"]["entry_channel"]

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
TRADE_LOG_OUTPUT_DIR = VAULT_ROOT / "【10】实盘记录" / "交易日志"  # 买入卡等建仓文档输出目录

ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "32500"))  # 默认本金 3.25 万（2026-07 实盘），env 可覆盖
DRAWDOWN_STATE = os.getenv("DRAWDOWN_STATE", "Normal")

# --- 回撤状态阈值（投资体系 V5.0 §6，相对初始权益的峰值回撤 %） ---
# 近似口径：累计 R × 平均风险率（见 review/monitor.py derive_drawdown_state）
DRAWDOWN_THRESHOLDS = {
    "Caution": 6.0,     # 回撤 6%：风险减半
    "Defensive": 8.0,   # 回撤 8%：防御收缩
    "Review": 12.0,     # 回撤 12%：停止实盘，全面复盘
}

# 月度回撤轨道（%，当月已实现回撤触线后的动作标记）
MONTHLY_DRAWDOWN_LIMITS = {
    "event_trade_halt": -4.0,   # 月度回撤 -4%：停止事件交易
    "new_position_halt": -6.0,  # 月度回撤 -6%：停止开新仓
}

# 持仓退出通道周期（按入场系统关键字匹配：含 S1→10 日最低价，含 S2→20 日最低价）
# 值统一来自 STRATEGY_PARAMS（monitor / signal_tracker / buy_card 共用此表）
EXIT_CHANNEL_PERIODS = {
    "S1": STRATEGY_PARAMS["S1-A"]["exit_channel"],
    "S2": STRATEGY_PARAMS["S2-A"]["exit_channel"],
}

# --- 信号验证（pipeline/signal_tracker.py，突破信号入库/结算/统计） ---
SIGNAL_MAX_HOLDING_DAYS = 20  # 信号最大持有交易日数，到期按收盘价强制结算
SIGNAL_STATS_MIN_SAMPLE = 5   # 统计最小样本量，低于此值标注「样本不足」

# 信号最大持有交易日数（按系统覆盖；未列出的系统沿用 SIGNAL_MAX_HOLDING_DAYS）
SIGNAL_MAX_HOLDING_BY_SYSTEM = {"HOT-S": 5}  # 超短热点信号 5 个交易日强制结算

# --- 超短热点池（pipeline/hot_pool.py，1-5 天超短候选来源） ---
HOT_SECTOR_TOP_N = 3        # 板块相对强度前 N 的板块：取领涨股入池 + 报告主线摘要
HOT_POOL_MAX = 120          # 热点池总量上限（控制日线补抓量）
HOT_HISTORY_DAYS = 90       # 热点池日线补抓长度（交易日目标，超短不需要长历史）
HOT_SIGNAL_SYSTEM = "HOT-S"  # 超短热点信号系统标识（signals 表 system 列）

# --- 趋势扫描动态池（pipeline/trend_pool.py，强势板块成分股，趋势候选来源；V5.0 §4.2 板块共振） ---
TREND_SCAN_TOP_SECTORS = 5   # 相对强度前 N 的板块：取成分股入趋势池
TREND_POOL_MAX = 300         # 趋势池总量上限（控制日线补抓量）
TREND_HISTORY_DAYS = 120     # 趋势池日线补抓长度（交易日目标，覆盖 55 日通道 + 20 周均线）

# --- 三重滤网（pipeline/trend_filters.py，V5.0 §4.2/§4.3 可量化部分） ---
TREND_SECTOR_TOP_PCT = 0.20    # 板块相对强度排名前 20% 视为「板块共振」
TREND_VOLUME_MEDIAN_DAYS = 20  # 量能确认：当日成交额 ≥ 过去 N 日成交额中位数（S1-A 入场条件②）

# --- S1-A 系统1过滤（V5.0 §4.3，基于 signals 表历史结算记录） ---
FALSE_BREAKOUT_MAX = 3           # 连续 N 次同标的假突破（止损退出）触发冷却
FALSE_BREAKOUT_COOLDOWN_DAYS = 20  # 冷却自然日数（期内同标的同系统信号不再入库）

# --- 盘前操作清单（pipeline/daily_plan.py，V5.0 盘前「只保留 3—5 只重点候选」） ---
DAILY_PLAN_MAX_CANDIDATES = 5    # 明日操作计划买入候选上限（滤网全过者优先，按突破幅度排序）

# --- 候选催化分析（research/catalyst_analyzer.py，买入规则⑧事件/政策/业绩/技术突破/转型） ---
HOT_CATALYST_ENABLED = os.getenv("HOT_CATALYST_ENABLED", "true").lower() == "true"  # 扫描时对热点候选做催化分析（联网，失败降级人工核对）
HOT_CATALYST_MAX = 5         # 每日催化分析候选上限（按规则满足条数排序取前 N，控制耗时与 LLM 调用量）
CATALYST_ANNOUNCE_DAYS = 90  # 公告回溯天数

# --- 合规规则（投资体系 V5.0；限额已按 3.25 万小资金校准，2026-07） ---
MAX_SINGLE_RISK_PCT = 1.0

# 组合总热度上限（%）：未平仓风险率合计 + 本笔风险率 不得超过当前市场状态上限
# （V5.0 §5.6：正常 ≤3%，高风险市场 ≤1.5%，强趋势最高 ≤4%；D 系统性下跌禁止新开仓）
PORTFOLIO_HEAT_LIMITS = {"A": 4.0, "B": 3.0, "C": 1.5, "D": 0.0}

RISK_LIMITS_NORMAL = {
    "核心": 1.0,
    "核心复利": 1.0,
    "产业": 1.0,
    "产业趋势": 1.0,
    "创新药": 1.0,
    "事件": 1.0,
    "事件交易": 1.0,
    "实验": 0.5,
    "预埋": 0.15,
}

RISK_LIMITS_DRAWDOWN = {
    "核心": 0.5,
    "核心复利": 0.5,
    "产业": 0.5,
    "产业趋势": 0.5,
    "创新药": 0.5,
    "事件": 0.5,
    "事件交易": 0.5,
    "实验": 0.25,
    "预埋": 0.0,
}

POSITION_LIMITS = {
    "核心": 30.0,
    "核心复利": 30.0,
    "产业": 20.0,
    "产业趋势": 20.0,
    "创新药": 20.0,
    "创新药期权": 20.0,
    "事件": 60.0,
    "事件交易": 60.0,
    "事件驱动": 60.0,
    "实验": 15.0,
    "实验策略": 15.0,
}

# 小资金集中轮动（一次一只），簇限与单票上限对齐
RISK_CLUSTER_LIMITS = {
    "创新药": {"exposure": 60.0, "stop_risk": 1.0},
    "AI算力": {"exposure": 60.0, "stop_risk": 1.0},
    "半导体": {"exposure": 60.0, "stop_risk": 1.0},
    "能源资源": {"exposure": 60.0, "stop_risk": 1.0},
    "商业航天": {"exposure": 60.0, "stop_risk": 1.0},
    "单一事件链": {"exposure": 60.0, "stop_risk": 1.0},
}

# 禁买板块（代码前缀）：2026-08-07 用户决策放开创业板（原 2026-07 纪律「不买创业板」废止）
# 机制保留：重新配置前缀元组（如 ("300","301")）即恢复建仓闸门高级违规与纪律审计
BANNED_BOARD_PREFIXES = ()

# 纪律审计：闪电换仓判定阈值（分钟）——卖出后 N 分钟内买入视为计划外冲动换仓
DISCIPLINE_SWITCH_MINUTES = 30

# --- 六条硬规则（2026-08 实盘复盘定制，建仓闸门/监控/审计共用） ---
BANNED_NEW_STOCK_ENABLED = True   # 规则1：永久拉黑 N/C 字头新股次新（按股票名称前缀判定）
MAX_SINGLE_POSITION_PCT = 50.0    # 规则2a：单票仓位绝对上限（占权益 %，高级违规，独立于 POSITION_LIMITS）
MAX_OPEN_POSITIONS = 2            # 规则2b：同时持仓只数上限（含本笔，高级违规）
OPEN_CHASE_CUTOFF = "10:00"       # 规则3：入场时间早于该时点记「开盘追高」（审计+中级警告）
TRAILING_PROFIT_PCT = 3.0         # 规则4：移动止盈回落幅度 %（自入场后最高收盘价回落，建议类警报）
STOP_SUGGEST_MAX_PCT = 4.0        # 规则4：止损宽度超过该 % 中级警告（建议 -3%~-4%）
CONSECUTIVE_LOSS_HALT_COUNT = 2   # 规则5：最近 N 笔已平仓全亏触发停手
CONSECUTIVE_LOSS_HALT_DAYS = 1    # 规则5：停手自然日数（自最近退出日期起）
MAX_WEEKLY_ENTRIES = 2            # 规则6：每周（周一至当日）新开仓上限（含本笔，高级违规）

FORBIDDEN_IN_DRAWDOWN = {
    "Caution": ["预埋"],
    "Defensive": ["预埋", "事件", "事件交易", "实验"],
    "Review": ["预埋", "事件", "事件交易", "实验", "产业", "产业趋势"],
}

# --- 建仓链路（仓位计算 / from-scan 落库） ---
# 策略代码枚举（【11】统一体系/策略评估筛选框架.md §1.1 五策略清单，与投资体系 V5.0 对应）：
# 建仓 --system 校验与「非系统内交易」合规检查共用；list/show 等展示场景不校验历史值
STRATEGY_CODES = ["S1-A", "S2-A", "STR-A", "STR-B", "STR-C", "HOT-S"]

STRATEGY_INFO = {
    "S1-A":  {"名称": "20日突破（快速系统）", "适用账户": "产业/事件", "典型持有期": "2—8周"},
    "S2-A":  {"名称": "55日突破（慢速系统）", "适用账户": "核心/产业", "典型持有期": "3—18月"},
    "STR-A": {"名称": "创新药价值重估",       "适用账户": "产业",      "典型持有期": "3—12月"},
    "STR-B": {"名称": "事件驱动第二波",       "适用账户": "事件",      "典型持有期": "1—4周"},
    "STR-C": {"名称": "核心复利",             "适用账户": "核心",      "典型持有期": "1—5年"},
    "HOT-S": {"名称": "超短热点池",           "适用账户": "事件",      "典型持有期": "1—5天"},
}

# 账户类型中文枚举（投资体系 V5.0，仓位计算器 CLI 与合规检查共用）
ACCOUNT_TYPES = ["核心", "产业", "事件", "实验"]

# 入场系统 → 默认账户类型（from-scan --execute 建仓映射，可用 --account 覆盖）
SYSTEM_DEFAULT_ACCOUNT = {"S1": "产业", "S2": "核心"}

# 股票代码 → 风险簇 映射（值必须对齐 RISK_CLUSTER_LIMITS 的键，否则簇限额不生效；
# None = 已核对但不属于 V5.0 现有簇，建仓流程提示人工指定，记为「未指定」；可按需扩展）
INDUSTRY_MAP = {
    "600519": None,      # 贵州茅台：白酒消费，不在 V5.0 六簇内
    "000858": None,      # 五粮液：白酒消费，不在 V5.0 六簇内
    "300760": None,      # 迈瑞医疗：医疗器械，非「创新药」簇
    "688235": "创新药",
    "688331": "创新药",
    "300750": None,      # 宁德时代：电池制造；V5.0「能源资源」指石油/煤炭/黄金等周期资源，不归入
    "002594": None,      # 比亚迪：整车制造，不在 V5.0 六簇内
}

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
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_MODEL_PRO = os.getenv("DEEPSEEK_MODEL_PRO", "deepseek-v4-pro")

CUSTOM_LLM_API_KEY = os.getenv("CUSTOM_LLM_API_KEY", "")
CUSTOM_LLM_BASE_URL = os.getenv("CUSTOM_LLM_BASE_URL", "")
CUSTOM_LLM_MODEL = os.getenv("CUSTOM_LLM_MODEL", "")

LLM_CACHE_DIR = DATA_DIR / "llm_cache"
LLM_CACHE_TTL = 24 * 3600
LLM_PROVIDER_PRIORITY = ["kimi", "deepseek", "custom"]

# Kimi 长文模型（公告等 task_type=announcement 时使用；moonshot-v1 系列 2026-08-31 停服，默认 kimi-k2.6）
KIMI_MODEL_LONG = os.getenv("KIMI_MODEL_LONG", "kimi-k2.6")

# Kimi 深度推理模型（task_type=reasoning：龙头辨识等多维综合研判；kimi-k2.6 兼顾推理能力与费用）
KIMI_MODEL_REASONING = os.getenv("KIMI_MODEL_REASONING", "kimi-k2.6")

# --- 龙头深度推理（research/dragon_reasoner.py，系统自主辨龙头） ---
DRAGON_REASON_ENABLED = os.getenv("DRAGON_REASON_ENABLED", "true").lower() == "true"  # false 时跳过 LLM 推理，按量化评分排序
DRAGON_REASON_MAX = 5        # 每日深度推理候选上限（控制 LLM 输入 token 与费用）
DRAGON_EVIDENCE_MAX = 3      # 每日个股证据链深挖上限（K2.6 联网检索，控制调用成本）

# --- FastAPI 服务与定时调度 ---
SCHEDULER_ENABLED = os.getenv("ENABLE_SCHEDULER", "").lower() == "true"
SCHEDULER_TIME = os.getenv("SCHEDULER_TIME", "08:30")
# 自动复盘调度（server.py 定时 job）：每周五生成周报、每月最后一天生成月报
WEEKLY_REVIEW_TIME = os.getenv("WEEKLY_REVIEW_TIME", "15:45")
MONTHLY_REVIEW_TIME = os.getenv("MONTHLY_REVIEW_TIME", "16:00")
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8900"))
