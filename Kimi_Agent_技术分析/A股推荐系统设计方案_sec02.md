## 第2章 数据层设计

数据层是整个系统的地基，也是当前环境下风险最高的一层。2025—2026年免费爬虫生态持续劣化：东方财富自2025年起大幅升级反爬（封IP、验证码），社区共识为"批量爬虫路线不可持续"；北向资金自2024-05-13起取消盘中实时披露、2024-08-19起仅盘后披露成交总额与十大活跃股（来源：新浪财经，2024-08-18；quant.10jqka.com.cn，2026-06）。因此本章设计遵循三条原则：多源冗余（任何单一接口失效不导致系统停摆）、本地落库（所有数据进SQLite，历史可回溯、可复算）、故障降级（数据缺失时信号自动降级为"仅观察"并钉钉告警，绝不基于残缺数据生成操作信号）。

### 2.1 数据源选型矩阵

下表对四个候选数据源按实时性、稳定性、成本与用途进行对比，选型结论为"akshare为主研究源、tushare日线兜底、baostock做回测底座、RSSHub自建快讯"。

| 数据源 | 实时性 | 稳定性 | 成本 | 定位与用途 |
|---|---|---|---|---|
| akshare | 东财网页快照，延迟数秒 | 中低：2025年起东财反爬升级，接口可能随时失效 | 免费 | 涨停池、跌停池、龙虎榜、主力资金流（东财大单口径）、板块题材——适合研究，不宜实盘依赖 |
| tushare Pro | 日线（50次/分限频） | 高：官方维护、数据规范 | 120积分免费仅日线；2000积分≈200元/年；分钟线/新闻需单独付费 | 日线行情兜底与交叉校验 |
| baostock | 无实时行情；分钟线当日20:00入库 | 高 | 免费 | 日/分钟历史K线，回测数据底座 |
| RSSHub自建+东财快讯爬取+akshare全球快讯 | 分钟级（取决于轮询频率） | 中：自建路由可控，爬取端点受反爬影响 | 免费（服务器自备） | 财经快讯事件流，喂给事件驱动引擎 |

（来源：tushare.pro官方文档doc_id=290；pypi.org/project/baostock，2026-07；github.com/ourongxing/newsnow issues#355，2026-06）

矩阵取舍说明：不选efinance，因其自2025年上半年起受东财匿名调用限制、维护放缓（来源：知乎专栏，2025）；不选财联社第三方镜像，因旧端点已404失效，故以RSSHub自建路由为主、东财快讯限速爬取为辅、akshare全球快讯兜底。akshare接口最全，但本质是东财网页爬虫，反爬收紧时最先失效，故只承担"增强信号"职责，其专属数据缺失不允许阻断日线级基础流程。tushare免费档仅日线，对"盘前准备+盘后复盘"场景已够用，暂不升级；分钟级数据由baostock当日20:00后补齐，仅供复盘与回测。

### 2.2 多源冗余与降级策略

针对两类结构性风险（东财反爬、北向停披）与常规接口故障，设计如下降级策略表。降级动作由数据层的健康检查模块自动执行：每次采集任务完成后校验记录数与字段完整率，不达标即触发对应降级分支。

| 故障场景 | 检测口径 | 降级动作 | 信号影响 |
|---|---|---|---|
| akshare涨停池/板块接口被反爬（封IP、验证码） | 连续2次请求失败或返回为空 | 切换备用User-Agent与限速（≥3秒/次）；仍失败则用tushare日线涨跌幅≥9.8%近似涨停池 | 涨停家数类条件标记"数据降级" |
| akshare主力资金流失效 | 字段完整率<90% | 停用资金流条件，等待恢复 | 当日信号降级为"仅观察"+钉钉告警 |
| 北向资金盘中停披（2024年起结构性变化） | 永久状态，非故障 | 用东财主力资金（大单口径）+行业ETF资金流替代北向口径 | 资金轮动判断改用替代指标 |
| 快讯源全部失效 | 30分钟无新增快讯 | RSSHub→东财爬取→akshare全球快讯三级切换；全失败则暂停事件驱动 | 事件类买入条件（第8条）记0分并告警 |
| tushare积分/限频超限 | 返回限频错误码 | 当日日线改用akshare快照收盘后落库，次日补校验 | 不影响，仅延迟落库 |

降级策略的逻辑是"宁可漏信号，不可错信号"：买入8条件中有4条依赖akshare专属数据（板块涨幅前5、涨停家数、资金承接、事件催化），数据残缺时评分卡区分度失真，继续生成买入建议的危害大于暂停信号，故统一降级为"仅观察"并钉钉告警。北向替代方案需注意口径差异：东财主力资金为大单口径，与北向"外资持股"口径不同源，仅作资金轮动的代理变量而非等效替代，信号层应降低该条件权重（第3章评分卡体现）。

### 2.3 数据表设计（SQLite）

存储选型为SQLite 3：单机零部署、单文件易备份、日增数据量千行级远低于其性能上限。共6张核心表，覆盖第3章信号层所需的全部输入：日线行情、涨停池、板块涨幅、资金流、新闻事件、观察池状态。

```sql
-- 日线行情：tushare主源，akshare兜底；backfill标记降级数据
CREATE TABLE daily_quote (
    trade_date   TEXT NOT NULL,            -- 交易日 YYYY-MM-DD
    ts_code      TEXT NOT NULL,            -- 股票代码 如 600519.SH
    open REAL, high REAL, low REAL, close REAL,
    pct_chg      REAL,                     -- 涨跌幅 %
    vol          REAL,                     -- 成交量（手）
    amount       REAL,                     -- 成交额（千元）
    turnover     REAL,                     -- 换手率 %
    source       TEXT DEFAULT 'tushare',   -- tushare/akshare/baostock
    backfill     INTEGER DEFAULT 0,        -- 1=降级/补录数据
    PRIMARY KEY (trade_date, ts_code)
);

-- 涨停池：akshare stock_zt_pool_em，每日盘后快照
CREATE TABLE limit_pool (
    trade_date   TEXT NOT NULL,
    ts_code      TEXT NOT NULL,
    name         TEXT,
    limit_times  INTEGER,                  -- 连板数
    first_time   TEXT,                     -- 首次封板时间
    last_time    TEXT,                     -- 最后封板时间
    open_times   INTEGER,                  -- 炸板次数（衡量承接）
    sector       TEXT,                     -- 所属板块（东财口径）
    degraded     INTEGER DEFAULT 0,        -- 1=tushare近似推算
    PRIMARY KEY (trade_date, ts_code)
);

-- 板块日线：买入条件1/2（涨幅前5、涨停家数增加）的数据基础
CREATE TABLE sector_daily (
    trade_date   TEXT NOT NULL,
    sector_name  TEXT NOT NULL,            -- 东财板块名
    pct_chg      REAL,                     -- 板块涨幅 %
    rank_no      INTEGER,                  -- 当日板块涨幅排名
    up_count     INTEGER,                  -- 上涨家数
    limit_count  INTEGER,                  -- 涨停家数
    amount       REAL,                     -- 板块成交额
    etf_code     TEXT,                     -- 关联行业ETF（北向替代用）
    PRIMARY KEY (trade_date, sector_name)
);

-- 资金流：东财主力资金大单口径；北向停披后的代理变量
CREATE TABLE capital_flow (
    trade_date   TEXT NOT NULL,
    ts_code      TEXT NOT NULL,            -- 个股或ETF代码
    main_inflow  REAL,                     -- 主力净流入（万元）
    super_large  REAL,                     -- 超大单净流入
    large        REAL,                     -- 大单净流入
    source       TEXT DEFAULT 'akshare',
    PRIMARY KEY (trade_date, ts_code)
);

-- 新闻事件：快讯落库+LLM打标结果，事件驱动引擎输入
CREATE TABLE news_event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pub_time     TEXT NOT NULL,            -- 发布时间
    source       TEXT,                     -- rsshub/eastmoney/akshare
    title        TEXT,
    content      TEXT,
    llm_sectors  TEXT,                     -- LLM提取的受益板块 JSON数组
    llm_score    REAL,                     -- 事件重要度评分 0-10
    verified     INTEGER DEFAULT 0,        -- 1=已通过板块白名单校验
    created_at   TEXT DEFAULT (datetime('now','localtime'))
);

-- 观察池：买入8条件评分卡状态机，第3章核心表
CREATE TABLE watch_pool (
    ts_code      TEXT NOT NULL,
    add_date     TEXT NOT NULL,            -- 入池日期
    sector_name  TEXT,
    cond_score   INTEGER DEFAULT 0,        -- 当前满足条件数（满分8）
    status       TEXT DEFAULT 'candidate', -- candidate/watching/buyable/holding/exited/archived
    buy_date     TEXT,                     -- 买入日（触发后回填）
    exit_reason  TEXT,                     -- 出池原因
    updated_at   TEXT,
    PRIMARY KEY (ts_code, add_date)
);
```

表结构设计有三处取舍。其一，行情类表以(trade_date, ts_code)复合主键天然防重，配合INSERT OR REPLACE容忍调度器重跑，免除额外幂等逻辑。其二，source/backfill/degraded字段是降级策略在数据层的落点：评分卡读取时可直接过滤或降权降级数据，保证数据质量信息不在层间传递中丢失。其三，watch_pool采用单表状态机而非多表拆分——3万本金场景下持股不超过2只、观察池预计不超过30只，单表足以承载。SQLite单文件每日收盘后自动备份至网盘目录，备份成本为零。

### 2.4 调度时序

调度器采用APScheduler（Python进程内cron），部署于闲置电脑或低配云主机。时序设计直接服务短线操作场景：用户实际成交集中在09:25—09:56早盘时段，且曾在9:15—9:25集合竞价阶段被主力洗盘手法（挂单价±10%制造恐慌）干扰心态而错误卖出，因此集合竞价时段的定位是"只采数据、不生成任何操作信号"，竞价异动仅作记录，供9:30后由信号层结合量能综合判断。

| 时段 | 时间 | 任务 | 数据源 | 输出 |
|---|---|---|---|---|
| 盘前 | 08:30 | 拉取隔夜外盘/商品（WTI、黄金、美元指数）、快讯增量、LLM事件打标 | akshare全球快讯+RSSHub | news_event更新、盘前事件简报推送钉钉 |
| 集合竞价 | 09:15—09:25 | 每2分钟采集竞价快照（价格、匹配量），仅落库不出信号 | akshare快照 | 竞价异动记录；09:25竞价结果（高开/低开幅度）写入简报（集合竞价采集窗口；信号锁定期为9:15–9:30，见第4章R1） |
| 盘中 | 09:30—15:00 | 每5分钟轮询板块涨幅榜与观察池个股；10:00/13:30/14:30三次板块排名快照 | akshare（限速≥3秒） | sector_daily增量、观察池条件实时更新 |
| 盘后① | 15:30 | 涨停池、主力资金流、板块日线落库；tushare日线兜底校验 | akshare+tushare | limit_pool/capital_flow/sector_daily/daily_quote |
| 盘后② | 20:00 | baostock分钟线补库；执行降级健康检查；生成当日复盘数据包 | baostock | 分钟K线落库、降级告警、复盘简报 |

时序安排有两点依据。第一，盘中轮询定为5分钟而非实时：东财快照本身延迟数秒，高频请求会加速封IP，5分钟粒度对"板块涨幅前5"这类分钟级不敏感条件已足够，日请求量控制在百次级。第二，盘后拆为15:30与20:00两批：baostock分钟线当日20:00才入库（来源：pypi.org/project/baostock，2026-07），合并会迫使涨停池等关键数据延后；且15:30批次若发现akshare数据残缺，20:00批次尚有补救重试窗口，与2.2节健康检查形成闭环。
