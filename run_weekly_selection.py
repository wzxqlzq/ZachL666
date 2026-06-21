import argparse
import csv
from datetime import date
from pathlib import Path

from offline_data import OfflineDataStore, OfflineMarketDataProvider
from stock_selector import RuleBasedStockSelector


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="data/offline")
    parser.add_argument("--output", default=f"selection_results/weekly_{date.today().isoformat()}.csv")
    args = parser.parse_args()

    store = OfflineDataStore(args.root)
    selector = RuleBasedStockSelector(
        universe_csv_path=str(store.universe_path),
        market_data=OfflineMarketDataProvider(store),
    )
    candidates = selector.select(date.today())

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "name", "reason"])
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(
                {
                    "symbol": candidate.symbol,
                    "name": candidate.name,
                    "reason": candidate.reason,
                }
            )

    print(f"Selected: {len(candidates)}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
