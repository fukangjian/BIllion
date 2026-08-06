"""
交易日志管理 — Trade 数据类与 JSON 存储
"""
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import TRADES_FILE

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """单笔交易记录"""
    交易编号: str = ""
    日期: str = ""
    入场时间: str = ""  # 入场时间 HH:MM:SS（可选，券商导入/手工补录；纪律审计用）
    股票代码: str = ""
    股票名称: str = ""
    账户类型: str = ""
    风险簇: str = ""
    入场系统: str = ""
    核心逻辑: str = ""
    入场价: float = 0.0
    止损价: float = 0.0
    风险率: float = 0.0
    股数: int = 0
    仓位金额: float = 0.0
    实际退出价: Optional[float] = None
    退出日期: Optional[str] = None
    退出时间: str = ""  # 退出时间 HH:MM:SS（可选，券商导入/手工补录；纪律审计用）
    退出原因: str = ""
    是否系统内交易: bool = True
    MFE: Optional[float] = None
    MAE: Optional[float] = None
    R倍数: Optional[float] = None
    关联单号: str = ""   # 加仓子单/部分平仓拆分子单指向来源交易编号（金字塔与分批退出用）
    单位序号: int = 1    # 金字塔单位序号：1=首仓，2/3=加仓单位（V5.0 §5.5 三档）
    备注: str = ""
    创建时间: str = field(default_factory=lambda: datetime.now().isoformat())
    更新时间: str = field(default_factory=lambda: datetime.now().isoformat())

    def __post_init__(self):
        if not self.交易编号:
            self.交易编号 = f"T{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"
        if not self.日期:
            self.日期 = datetime.now().strftime("%Y-%m-%d")

    @property
    def is_closed(self) -> bool:
        return self.实际退出价 is not None and self.退出日期 is not None

    @property
    def per_share_risk(self) -> float:
        if self.入场价 <= 0 or self.止损价 <= 0:
            return 0.0
        return abs(self.入场价 - self.止损价)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Trade":
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


class TradeLog:
    """交易日志 JSON 存储管理"""

    def __init__(self, file_path: Path | None = None):
        self.file_path = file_path or TRADES_FILE
        self._ensure_data_dir()

    def _ensure_data_dir(self):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self._save_all([])

    def _load_all(self) -> list[dict]:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            logger.warning("交易日志文件损坏或不存在，初始化为空")
            return []

    def _save_all(self, trades: list[dict]):
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(trades, f, ensure_ascii=False, indent=2)

    def add(self, trade: Trade) -> Trade:
        trades = self._load_all()
        trade.创建时间 = datetime.now().isoformat()
        trade.更新时间 = trade.创建时间
        trades.append(trade.to_dict())
        self._save_all(trades)
        logger.info("已添加交易: %s %s", trade.交易编号, trade.股票代码)
        return trade

    def get(self, trade_id: str) -> Optional[Trade]:
        for data in self._load_all():
            if data.get("交易编号") == trade_id:
                return Trade.from_dict(data)
        return None

    def get_by_symbol(self, symbol: str) -> list[Trade]:
        symbol = symbol.zfill(6)[-6:]
        return [
            Trade.from_dict(d)
            for d in self._load_all()
            if d.get("股票代码", "").zfill(6)[-6:] == symbol
        ]

    def list_all(self, closed_only: bool = False, open_only: bool = False) -> list[Trade]:
        trades = [Trade.from_dict(d) for d in self._load_all()]
        if closed_only:
            trades = [t for t in trades if t.is_closed]
        if open_only:
            trades = [t for t in trades if not t.is_closed]
        return trades

    def list_by_date_range(self, start: str, end: str) -> list[Trade]:
        trades = self.list_all()
        return [t for t in trades if start <= t.日期 <= end]

    def update(self, trade_id: str, **kwargs) -> Optional[Trade]:
        trades = self._load_all()
        for i, data in enumerate(trades):
            if data.get("交易编号") == trade_id:
                data.update(kwargs)
                data["更新时间"] = datetime.now().isoformat()
                trades[i] = data
                self._save_all(trades)
                logger.info("已更新交易: %s", trade_id)
                return Trade.from_dict(data)
        logger.warning("未找到交易: %s", trade_id)
        return None

    def delete(self, trade_id: str) -> bool:
        trades = self._load_all()
        new_trades = [d for d in trades if d.get("交易编号") != trade_id]
        if len(new_trades) == len(trades):
            return False
        self._save_all(new_trades)
        logger.info("已删除交易: %s", trade_id)
        return True

    def count(self) -> int:
        return len(self._load_all())
