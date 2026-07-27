"""
产业链映射 — 硬编码框架 + LLM 扩展，输出上中下游公司与催化
"""
import argparse
import logging
from datetime import datetime
from pathlib import Path

from config import INDUSTRY_OUTPUT_DIR
from llm_client import call_llm, has_llm_api_key
from prompts import INDUSTRY_MAPPING, INDUSTRY_MAPPING_SYSTEM
from utils import obsidian_frontmatter, safe_fetch, write_markdown

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 硬编码产业链框架
INDUSTRY_CHAINS: dict[str, dict] = {
    "创新药": {
        "upstream": ["原料药(CMO/CDMO)", "临床CRO", "实验动物", "试剂耗材"],
        "midstream": ["创新药企(Biotech)", "仿制药企", "疫苗/血制品"],
        "downstream": ["医药流通", "医院/连锁药房", "医保支付"],
        "companies": {
            "上游": ["603259 药明康德", "300347 泰格医药", "688131 皓元医药"],
            "中游": ["688192 迪哲医药", "688235 百济神州", "688180 君实生物", "600276 恒瑞医药"],
            "下游": ["603939 益丰药房", "601607 上海医药"],
        },
        "catalysts": ["医保谈判", "临床数据读出", "BD授权", "FDA/NMPA审批"],
    },
    "AI算力": {
        "upstream": ["AI芯片(GPU/ASIC)", "先进封装", "HBM存储", "光模块"],
        "midstream": ["服务器/AI一体机", "数据中心", "云计算"],
        "downstream": ["大模型应用", "AI+行业解决方案", "智能终端"],
        "companies": {
            "上游": ["688256 寒武纪", "002371 北方华创", "300308 中际旭创", "603986 兆易创新"],
            "中游": ["000977 浪潮信息", "603019 中科曙光", "300474 景嘉微"],
            "下游": ["688111 金山办公", "002230 科大讯飞"],
        },
        "catalysts": ["国产算力政策", "大模型迭代", "算力租赁价格", "出口管制变化"],
    },
    "半导体": {
        "upstream": ["EDA/IP", "半导体设备", "半导体材料"],
        "midstream": ["晶圆制造", "封装测试", "芯片设计"],
        "downstream": ["消费电子", "汽车电子", "工业/通信"],
        "companies": {
            "上游": ["688012 中微公司", "002371 北方华创", "688126 沪硅产业"],
            "中游": ["688981 中芯国际", "603501 韦尔股份", "688008 澜起科技"],
            "下游": ["002475 立讯精密", "601138 工业富联"],
        },
        "catalysts": ["国产替代进度", "库存周期", "消费电子复苏", "汽车智能化"],
    },
    "新能源": {
        "upstream": ["锂/钴/镍资源", "正极/负极/电解液/隔膜"],
        "midstream": ["电池制造", "光伏组件", "风电设备"],
        "downstream": ["储能系统", "新能源汽车", "电网/运营商"],
        "companies": {
            "上游": ["002460 赣锋锂业", "300750 宁德时代供应链"],
            "中游": ["300750 宁德时代", "601012 隆基绿能", "002594 比亚迪"],
            "下游": ["300274 阳光电源", "688390 固德威"],
        },
        "catalysts": ["碳酸锂价格", "装机量数据", "补贴政策", "海外贸易壁垒"],
    },
}


def get_chain_structure(keyword: str) -> dict:
    """获取产业链结构，支持模糊匹配"""
    keyword = keyword.strip()
    if keyword in INDUSTRY_CHAINS:
        return INDUSTRY_CHAINS[keyword]

    for name, chain in INDUSTRY_CHAINS.items():
        if keyword in name or name in keyword:
            return chain

    # 未知产业，返回空框架
    return {
        "upstream": ["上游环节（待 LLM 补充）"],
        "midstream": ["中游环节（待 LLM 补充）"],
        "downstream": ["下游环节（待 LLM 补充）"],
        "companies": {},
        "catalysts": [],
    }


def fetch_market_context(keyword: str) -> str:
    """获取近期板块行情作为背景"""
    try:
        import akshare as ak

        df = safe_fetch(ak.stock_board_industry_name_em, default=None)
        if df is not None and not df.empty:
            matched = df[df["板块名称"].str.contains(keyword[:2], na=False)]
            if not matched.empty:
                row = matched.iloc[0]
                return (
                    f"板块: {row.get('板块名称', '')}, "
                    f"涨跌幅: {row.get('涨跌幅', 'N/A')}%, "
                    f"成交额: {row.get('成交额', 'N/A')}"
                )
    except Exception as e:
        logger.debug("获取板块背景失败: %s", e)
    return "（暂无实时板块数据）"


def format_chain_text(chain: dict) -> str:
    """格式化产业链结构为文本"""
    lines = [
        f"上游: {', '.join(chain.get('upstream', []))}",
        f"中游: {', '.join(chain.get('midstream', []))}",
        f"下游: {', '.join(chain.get('downstream', []))}",
        f"常见催化: {', '.join(chain.get('catalysts', []))}",
    ]
    return "\n".join(lines)


def format_companies_text(chain: dict) -> str:
    """格式化代表性公司"""
    companies = chain.get("companies", {})
    if not companies:
        return "（暂无预置公司，由 LLM 补充）"
    lines = []
    for segment, lst in companies.items():
        lines.append(f"**{segment}**: {', '.join(lst)}")
    return "\n".join(lines)


def analyze_industry(keyword: str, chain: dict, context: str) -> str:
    """LLM 扩展产业链分析"""
    if not has_llm_api_key():
        return _fallback_analysis(keyword, chain)

    prompt = INDUSTRY_MAPPING.format(
        keyword=keyword,
        chain_structure=format_chain_text(chain),
        companies=format_companies_text(chain),
        context=context,
    )
    result = call_llm(prompt, system_prompt=INDUSTRY_MAPPING_SYSTEM)
    return result or _fallback_analysis(keyword, chain)


def _fallback_analysis(keyword: str, chain: dict) -> str:
    """无 LLM 时的降级输出"""
    lines = [
        "## 产业链全景",
        format_chain_text(chain),
        "",
        "## 各环节核心公司",
        format_companies_text(chain),
        "",
        "## 近期政策与事件催化",
        "\n".join(f"- {c}" for c in chain.get("catalysts", ["待补充"])),
        "",
        "> ⚠️ 配置 LLM API 密钥后可获得更完整的扩展分析。",
    ]
    return "\n".join(lines)


def generate_report(keyword: str, output_dir: Path | None = None) -> Path:
    """生成产业链映射 Markdown 报告"""
    output_dir = output_dir or INDUSTRY_OUTPUT_DIR
    today = datetime.now().strftime("%Y-%m-%d")

    chain = get_chain_structure(keyword)
    context = fetch_market_context(keyword)
    analysis = analyze_industry(keyword, chain, context)

    lines = [
        obsidian_frontmatter(
            ["AI研究", "产业链映射"],
            keyword=keyword,
            date=today,
        ),
        f"# {keyword} 产业链映射",
        "",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"> 市场背景: {context}",
        "",
        analysis,
    ]

    safe_keyword = keyword.replace("/", "_").replace("\\", "_")
    output_path = output_dir / f"{today}_产业链_{safe_keyword}.md"
    return write_markdown("\n".join(lines), output_path)


def main():
    parser = argparse.ArgumentParser(description="产业链映射助手")
    parser.add_argument("keyword", help="产业关键词，如 创新药、AI算力、半导体")
    parser.add_argument("-o", "--output", type=str, default=None, help="输出目录")
    args = parser.parse_args()

    output_dir = Path(args.output) if args.output else None
    path = generate_report(args.keyword, output_dir=output_dir)
    print(f"报告已生成: {path}")


if __name__ == "__main__":
    main()
