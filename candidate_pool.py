import csv
from pathlib import Path

from models import Position, StockCandidate


class CandidatePoolRepository:
    fieldnames = ["symbol", "name", "reason", "status"]

    def __init__(self, csv_path: str):
        self.csv_path = Path(csv_path)

    def replace_with_weekly_selection(
        self,
        selected: list[StockCandidate],
        positions: dict[str, Position],
    ) -> list[StockCandidate]:
        rows: dict[str, dict[str, object]] = {}

        for candidate in selected:
            self._upsert(rows, candidate.symbol, candidate.name, "weekly_selection")

        for key, position in positions.items():
            if not self._is_active_position(position):
                continue
            symbol = position.symbol or key
            self._upsert(rows, symbol, "", "portfolio_position")

        candidates = [
            StockCandidate(
                symbol=str(row["symbol"]),
                name=str(row["name"]),
                reason=";".join(row["reasons"]),
            )
            for row in rows.values()
        ]
        self._write(candidates)
        return candidates

    def _upsert(
        self,
        rows: dict[str, dict[str, object]],
        symbol: str,
        name: str,
        reason: str,
    ) -> None:
        symbol = symbol.strip()
        if not symbol:
            return

        row = rows.setdefault(symbol, {"symbol": symbol, "name": "", "reasons": []})
        if name and not row["name"]:
            row["name"] = name.strip()

        reasons = row["reasons"]
        if reason and reason not in reasons:
            reasons.append(reason)

    def _write(self, candidates: list[StockCandidate]) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.fieldnames)
            writer.writeheader()
            for candidate in candidates:
                writer.writerow(
                    {
                        "symbol": candidate.symbol,
                        "name": candidate.name,
                        "reason": candidate.reason,
                        "status": "",
                    }
                )

    def _is_active_position(self, position: Position) -> bool:
        return position.shares > 0 or position.strategy_status.upper() == "LONG"
