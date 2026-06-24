from datetime import date, datetime

from interfaces import MarketDataProvider, StockSelector, StrategyEngine, TradeGateway
from models import Position, StockCandidate


class Runner:
    def __init__(
        self,
        selector: StockSelector,
        market_data: MarketDataProvider,
        strategy: StrategyEngine,
        trade_gateway: TradeGateway,
        positions: dict[str, Position],
        portfolio_repository=None,
    ):
        self.selector = selector
        self.market_data = market_data
        self.strategy = strategy
        self.trade_gateway = trade_gateway
        self.positions = positions
        self.portfolio_repository = portfolio_repository

    def run_once(self, as_of: date | None = None, at: datetime | None = None) -> int:
        run_date = as_of or date.today()
        emitted = 0
        for candidate in self._scan_candidates(run_date):
            bars = self.market_data.load_daily_bars(candidate.symbol, end_date=run_date)
            quote = self.market_data.get_quote(candidate.symbol, at=at)
            position = self.positions.get(candidate.symbol, Position(symbol=candidate.symbol))
            for signal in self.strategy.on_quote(candidate, bars, quote, position):
                intent = self.trade_gateway.submit_signal(signal)
                if intent.status == "NEW":
                    self._apply_strategy_signal(signal)
                emitted += 1
        return emitted

    def _scan_candidates(self, run_date: date) -> list[StockCandidate]:
        candidates = list(self.selector.select(run_date))
        seen = {candidate.symbol for candidate in candidates}

        for symbol, position in self.positions.items():
            position_symbol = position.symbol or symbol
            if position_symbol in seen:
                continue
            if not self._is_active_position(position):
                continue
            candidates.append(
                StockCandidate(
                    symbol=position_symbol,
                    reason="portfolio_position",
                )
            )
            seen.add(position_symbol)
        return candidates

    def _is_active_position(self, position: Position) -> bool:
        return position.shares > 0 or position.strategy_status.upper() == "LONG"

    def _apply_strategy_signal(self, signal) -> None:
        if self.portfolio_repository is not None:
            self.positions[signal.symbol] = self.portfolio_repository.apply_strategy_signal(signal)
            return

        current = self.positions.get(signal.symbol, Position(symbol=signal.symbol))
        if signal.action == "BUY":
            self.positions[signal.symbol] = Position(
                symbol=current.symbol,
                shares=current.shares,
                avg_cost=current.avg_cost,
                buy_date=current.buy_date,
                strategy_status="LONG",
                strategy_entry_date=signal.trade_date,
                strategy_entry_price=signal.price,
                strategy_stop_loss=signal.stop_loss,
            )
        elif signal.action == "SELL":
            self.positions[signal.symbol] = Position(
                symbol=current.symbol,
                shares=current.shares,
                avg_cost=current.avg_cost,
                buy_date=current.buy_date,
                strategy_status="FLAT",
                strategy_entry_date=current.strategy_entry_date,
                strategy_entry_price=current.strategy_entry_price,
                strategy_stop_loss=current.strategy_stop_loss,
            )
