import json
from datetime import datetime
from pathlib import Path

from models import Position, Signal


class PortfolioRepository:
    def __init__(self, path: str):
        self.path = Path(path)

    def _load_data(self) -> dict:
        if not self.path.exists():
            return {"cash": 0, "positions": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def load_positions(self) -> dict[str, Position]:
        data = self._load_data()
        positions: dict[str, Position] = {}
        for symbol, raw in (data.get("positions") or {}).items():
            buy_date = raw.get("buy_date")
            strategy_entry_date = raw.get("strategy_entry_date")
            positions[symbol] = Position(
                symbol=symbol,
                shares=int(raw.get("shares") or 0),
                avg_cost=float(raw.get("avg_cost") or 0),
                buy_date=datetime.strptime(buy_date, "%Y-%m-%d").date() if buy_date else None,
                strategy_status=(raw.get("strategy_status") or "FLAT").upper(),
                strategy_entry_date=(
                    datetime.strptime(strategy_entry_date, "%Y-%m-%d").date()
                    if strategy_entry_date
                    else None
                ),
                strategy_entry_price=(
                    float(raw["strategy_entry_price"])
                    if raw.get("strategy_entry_price") not in {None, ""}
                    else None
                ),
                strategy_stop_loss=(
                    float(raw["strategy_stop_loss"])
                    if raw.get("strategy_stop_loss") not in {None, ""}
                    else None
                ),
            )
        return positions

    def apply_strategy_signal(self, signal: Signal) -> Position:
        data = self._load_data()
        raw_positions = data.setdefault("positions", {})
        raw = raw_positions.setdefault(
            signal.symbol,
            {
                "shares": 0,
                "avg_cost": 0,
                "buy_date": None,
            },
        )

        if signal.action == "BUY":
            raw["strategy_status"] = "LONG"
            raw["strategy_entry_date"] = signal.trade_date.isoformat()
            raw["strategy_entry_price"] = signal.price
            raw["strategy_stop_loss"] = signal.stop_loss
        elif signal.action == "SELL":
            raw["strategy_status"] = "FLAT"
            raw["strategy_exit_date"] = signal.trade_date.isoformat()
            raw["strategy_exit_price"] = signal.price

        self._write_data(data)
        return self.load_positions()[signal.symbol]

    def _write_data(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
