from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Bar:
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float | None = None


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime
    price: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: int | None = None


@dataclass(frozen=True)
class StockCandidate:
    symbol: str
    name: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Position:
    symbol: str
    shares: int = 0
    avg_cost: float = 0.0
    buy_date: date | None = None
    strategy_status: str = "FLAT"
    strategy_entry_date: date | None = None
    strategy_entry_price: float | None = None


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: str
    trade_date: date
    price: float
    reason: str
    risk_note: str = ""
    confirmed_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.trade_date.isoformat()}:{self.symbol}:{self.action}"


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    action: str
    trade_date: date
    created_at: datetime
    reference_price: float
    reason: str
    risk_note: str
    status: str = "NEW"
