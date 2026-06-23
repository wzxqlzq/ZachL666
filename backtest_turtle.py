import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from models import Bar


@dataclass(frozen=True)
class Trade:
    symbol: str
    name: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float

    @property
    def return_pct(self) -> float:
        return self.exit_price / self.entry_price - 1

    @property
    def holding_days(self) -> int:
        return (self.exit_date - self.entry_date).days


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    name: str
    entry_date: date
    entry_price: float
    latest_date: date
    latest_price: float

    @property
    def return_pct(self) -> float:
        return self.latest_price / self.entry_price - 1


def read_candidates(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [
            {
                "symbol": (row.get("symbol") or "").strip(),
                "name": (row.get("name") or "").strip(),
            }
            for row in csv.DictReader(f)
            if (row.get("symbol") or "").strip()
        ]


def read_bars(path: Path, start_date: date | None, end_date: date | None) -> list[Bar]:
    bars: list[Bar] = []
    if not path.exists():
        return bars
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            trade_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
            if start_date and trade_date < start_date:
                continue
            if end_date and trade_date > end_date:
                continue
            bars.append(
                Bar(
                    symbol=row["symbol"],
                    trade_date=trade_date,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row["volume"])),
                    amount=float(row["amount"]) if row.get("amount") else None,
                )
            )
    return sorted(bars, key=lambda item: item.trade_date)


def backtest_symbol(
    symbol: str,
    name: str,
    bars: list[Bar],
    entry_window: int,
    exit_window: int,
) -> tuple[list[Trade], OpenPosition | None]:
    trades: list[Trade] = []
    in_position = False
    entry_date: date | None = None
    entry_price = 0.0

    for index, bar in enumerate(bars):
        if index < max(entry_window, exit_window):
            continue

        previous_bars = bars[:index]
        entry_high = max(item.high for item in previous_bars[-entry_window:])
        exit_low = min(item.low for item in previous_bars[-exit_window:])

        if not in_position and bar.close > entry_high:
            in_position = True
            entry_date = bar.trade_date
            entry_price = bar.close
            continue

        if in_position and bar.close < exit_low:
            trades.append(
                Trade(
                    symbol=symbol,
                    name=name,
                    entry_date=entry_date or bar.trade_date,
                    entry_price=entry_price,
                    exit_date=bar.trade_date,
                    exit_price=bar.close,
                )
            )
            in_position = False
            entry_date = None
            entry_price = 0.0

    open_position = None
    if in_position and bars:
        latest = bars[-1]
        open_position = OpenPosition(
            symbol=symbol,
            name=name,
            entry_date=entry_date or latest.trade_date,
            entry_price=entry_price,
            latest_date=latest.trade_date,
            latest_price=latest.close,
        )
    return trades, open_position


def pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def summarize(trades: list[Trade], open_positions: list[OpenPosition]) -> list[str]:
    closed_returns = [trade.return_pct for trade in trades]
    wins = [value for value in closed_returns if value > 0]
    losses = [value for value in closed_returns if value <= 0]
    open_returns = [position.return_pct for position in open_positions]

    lines = [
        f"closed_trades={len(trades)}",
        f"open_positions={len(open_positions)}",
    ]
    if closed_returns:
        lines.extend(
            [
                f"win_rate={len(wins) / len(closed_returns):.2%}",
                f"avg_closed_return={pct(sum(closed_returns) / len(closed_returns))}",
                f"best_closed_return={pct(max(closed_returns))}",
                f"worst_closed_return={pct(min(closed_returns))}",
            ]
        )
    if open_returns:
        lines.append(f"avg_open_return={pct(sum(open_returns) / len(open_returns))}")
    return lines


def write_trades(path: Path, trades: list[Trade], open_positions: list[OpenPosition]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "symbol",
                "name",
                "status",
                "entry_date",
                "entry_price",
                "exit_date",
                "exit_price",
                "return_pct",
                "holding_days",
            ],
        )
        writer.writeheader()
        for trade in trades:
            writer.writerow(
                {
                    "symbol": trade.symbol,
                    "name": trade.name,
                    "status": "CLOSED",
                    "entry_date": trade.entry_date.isoformat(),
                    "entry_price": f"{trade.entry_price:.4f}",
                    "exit_date": trade.exit_date.isoformat(),
                    "exit_price": f"{trade.exit_price:.4f}",
                    "return_pct": pct(trade.return_pct),
                    "holding_days": trade.holding_days,
                }
            )
        for position in open_positions:
            writer.writerow(
                {
                    "symbol": position.symbol,
                    "name": position.name,
                    "status": "OPEN",
                    "entry_date": position.entry_date.isoformat(),
                    "entry_price": f"{position.entry_price:.4f}",
                    "exit_date": "",
                    "exit_price": f"{position.latest_price:.4f}",
                    "return_pct": pct(position.return_pct),
                    "holding_days": (position.latest_date - position.entry_date).days,
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", default="selection_results/weekly_2026-06-22.csv")
    parser.add_argument("--data-root", default="data/offline/daily_bars")
    parser.add_argument("--output", default="selection_results/backtest_turtle.csv")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--entry-window", type=int, default=55)
    parser.add_argument("--exit-window", type=int, default=20)
    args = parser.parse_args()

    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date() if args.start_date else None
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else None
    data_root = Path(args.data_root)

    trades: list[Trade] = []
    open_positions: list[OpenPosition] = []
    missing = 0
    too_short = 0
    candidates = read_candidates(Path(args.candidates))

    for candidate in candidates:
        symbol = candidate["symbol"]
        bars = read_bars(data_root / f"{symbol}.csv", start_date, end_date)
        if not bars:
            missing += 1
            continue
        if len(bars) <= max(args.entry_window, args.exit_window):
            too_short += 1
            continue
        symbol_trades, open_position = backtest_symbol(
            symbol=symbol,
            name=candidate["name"],
            bars=bars,
            entry_window=args.entry_window,
            exit_window=args.exit_window,
        )
        trades.extend(symbol_trades)
        if open_position is not None:
            open_positions.append(open_position)

    write_trades(Path(args.output), trades, open_positions)
    print(f"candidates={len(candidates)}")
    print(f"missing_bars={missing}")
    print(f"too_short={too_short}")
    for line in summarize(trades, open_positions):
        print(line)
    print(f"output={args.output}")


if __name__ == "__main__":
    main()
