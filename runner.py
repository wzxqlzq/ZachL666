from datetime import date, datetime

from interfaces import MarketDataProvider, StockSelector, StrategyEngine, TradeGateway
from models import Position


class Runner:
    def __init__(
        self,
        selector: StockSelector,
        market_data: MarketDataProvider,
        strategy: StrategyEngine,
        trade_gateway: TradeGateway,
        positions: dict[str, Position],
    ):
        self.selector = selector
        self.market_data = market_data
        self.strategy = strategy
        self.trade_gateway = trade_gateway
        self.positions = positions

    def run_once(self, as_of: date | None = None, at: datetime | None = None) -> int:
        run_date = as_of or date.today()
        emitted = 0
        for candidate in self.selector.select(run_date):
            bars = self.market_data.load_daily_bars(candidate.symbol, end_date=run_date)
            quote = self.market_data.get_quote(candidate.symbol, at=at)
            position = self.positions.get(candidate.symbol, Position(symbol=candidate.symbol))
            for signal in self.strategy.on_quote(candidate, bars, quote, position):
                self.trade_gateway.submit_signal(signal)
                emitted += 1
        return emitted
