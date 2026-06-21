from datetime import date, datetime
from typing import Protocol

from models import Bar, OrderIntent, Position, Quote, Signal, StockCandidate


class StockSelector(Protocol):
    def select(self, as_of: date) -> list[StockCandidate]:
        ...


class MarketDataProvider(Protocol):
    def load_daily_bars(self, symbol: str, end_date: date | None = None) -> list[Bar]:
        ...

    def get_quote(self, symbol: str, at: datetime | None = None) -> Quote:
        ...


class StrategyEngine(Protocol):
    def on_quote(
        self,
        candidate: StockCandidate,
        daily_bars: list[Bar],
        quote: Quote,
        position: Position,
    ) -> list[Signal]:
        ...


class TradeGateway(Protocol):
    def submit_signal(self, signal: Signal) -> OrderIntent:
        ...
