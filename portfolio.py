import json
from datetime import datetime
from pathlib import Path

from models import Position


class PortfolioRepository:
    def __init__(self, path: str):
        self.path = Path(path)

    def load_positions(self) -> dict[str, Position]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        positions: dict[str, Position] = {}
        for symbol, raw in (data.get("positions") or {}).items():
            buy_date = raw.get("buy_date")
            positions[symbol] = Position(
                symbol=symbol,
                shares=int(raw.get("shares") or 0),
                avg_cost=float(raw.get("avg_cost") or 0),
                buy_date=datetime.strptime(buy_date, "%Y-%m-%d").date() if buy_date else None,
            )
        return positions
