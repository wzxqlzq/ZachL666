from datetime import datetime, timedelta

from models import Bar, Position, Quote, Signal, StockCandidate
from position_sizing import AtrPositionSizer, PositionSize


class TurtleStrategyEngine:
    def __init__(
        self,
        entry_window: int,
        exit_window: int,
        atr_window: int,
        risk_per_trade: float,
        confirm_minutes: int = 5,
        account_equity: float = 1_000_000,
        lot_size: int = 100,
        stop_atr_multiple: float = 2.0,
    ):
        self.entry_window = entry_window
        self.exit_window = exit_window
        self.atr_window = atr_window
        self.risk_per_trade = risk_per_trade
        self.confirm_delay = timedelta(minutes=confirm_minutes)
        self.position_sizer = AtrPositionSizer(
            account_equity=account_equity,
            risk_per_trade=risk_per_trade,
            lot_size=lot_size,
            stop_atr_multiple=stop_atr_multiple,
        )
        self._pending: dict[tuple[str, str], datetime] = {}
        self._emitted: set[str] = set()

    def on_quote(
        self,
        candidate: StockCandidate,
        daily_bars: list[Bar],
        quote: Quote,
        position: Position,
    ) -> list[Signal]:
        completed_bars = sorted(
            [bar for bar in daily_bars if bar.trade_date < quote.timestamp.date()],
            key=lambda bar: bar.trade_date,
        )
        if len(completed_bars) < max(self.entry_window, self.exit_window, self.atr_window):
            return []

        entry_high = max(bar.high for bar in completed_bars[-self.entry_window :])
        exit_low = min(bar.low for bar in completed_bars[-self.exit_window :])
        atr = self._atr(completed_bars[-(self.atr_window + 1) :])

        if not self._has_strategy_position(position):
            return self._maybe_confirm_buy(candidate, quote, entry_high, atr)
        return self._maybe_confirm_sell(candidate, quote, position, exit_low)

    def _has_strategy_position(self, position: Position) -> bool:
        return position.shares > 0 or position.strategy_status.upper() == "LONG"

    def _maybe_confirm_buy(
        self,
        candidate: StockCandidate,
        quote: Quote,
        entry_high: float,
        atr: float,
    ) -> list[Signal]:
        if quote.price <= entry_high:
            self._pending.pop((candidate.symbol, "BUY"), None)
            return []
        size = self.position_sizer.size(quote.price, atr)
        return self._confirm_or_wait(
            symbol=candidate.symbol,
            action="BUY",
            quote=quote,
            reason=f"Breakout above {self.entry_window}-day high {entry_high:.2f}",
            risk_note=(
                f"Review manually. Suggested shares: {size.shares}. "
                f"ATR{self.atr_window}: {size.atr:.2f}. Stop loss: {size.stop_loss:.2f}. "
                f"Risk amount target: {size.risk_amount:.2f}."
            ),
            size=size,
        )

    def _maybe_confirm_sell(
        self,
        candidate: StockCandidate,
        quote: Quote,
        position: Position,
        exit_low: float,
    ) -> list[Signal]:
        stop_loss = position.strategy_stop_loss
        exit_low_triggered = quote.price < exit_low
        stop_loss_triggered = stop_loss is not None and quote.price <= stop_loss
        if not exit_low_triggered and not stop_loss_triggered:
            self._pending.pop((candidate.symbol, "SELL"), None)
            return []

        reasons = []
        if exit_low_triggered:
            reasons.append(f"Breakdown below {self.exit_window}-day low {exit_low:.2f}")
        if stop_loss_triggered:
            reasons.append(f"Hit fixed stop loss {stop_loss:.2f}")
        return self._confirm_or_wait(
            symbol=candidate.symbol,
            action="SELL",
            quote=quote,
            reason="; ".join(reasons),
            risk_note=f"Current position: {position.shares} shares. Confirm T+1 availability manually.",
        )

    def _confirm_or_wait(
        self,
        symbol: str,
        action: str,
        quote: Quote,
        reason: str,
        risk_note: str,
        size: PositionSize | None = None,
    ) -> list[Signal]:
        pending_key = (symbol, action)
        emitted_key = f"{quote.timestamp.date().isoformat()}:{symbol}:{action}"
        if emitted_key in self._emitted:
            return []

        if self.confirm_delay <= timedelta(0):
            return self._emit_signal(symbol, action, quote, reason, risk_note, pending_key, size)

        first_seen = self._pending.get(pending_key)
        if first_seen is None:
            self._pending[pending_key] = quote.timestamp
            return []

        if quote.timestamp - first_seen < self.confirm_delay:
            return []

        return self._emit_signal(symbol, action, quote, reason, risk_note, pending_key, size)

    def _emit_signal(
        self,
        symbol: str,
        action: str,
        quote: Quote,
        reason: str,
        risk_note: str,
        pending_key: tuple[str, str],
        size: PositionSize | None,
    ) -> list[Signal]:
        signal = Signal(
            symbol=symbol,
            action=action,
            trade_date=quote.timestamp.date(),
            price=quote.price,
            reason=reason,
            risk_note=risk_note,
            confirmed_at=quote.timestamp,
            suggested_shares=size.shares if size else None,
            suggested_notional=size.notional if size else None,
            atr=size.atr if size else None,
            stop_loss=size.stop_loss if size else None,
            risk_amount=size.risk_amount if size else None,
        )
        self._emitted.add(signal.key)
        self._pending.pop(pending_key, None)
        return [signal]

    def _atr(self, bars: list[Bar]) -> float:
        true_ranges: list[float] = []
        for previous, current in zip(bars, bars[1:]):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
        return sum(true_ranges[-self.atr_window :]) / self.atr_window


TurtleStrategy = TurtleStrategyEngine
