import argparse

from market_data import AkshareMarketDataProvider


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="data/stock_universe.csv", help="output stock universe CSV path")
    parser.add_argument("--lookback-days", type=int, default=220)
    parser.add_argument("--adjust", default="qfq")
    args = parser.parse_args()

    provider = AkshareMarketDataProvider(lookback_days=args.lookback_days, adjust=args.adjust)
    count = provider.export_stock_universe_csv(args.universe)
    print(f"Exported {count} stocks to {args.universe}")


if __name__ == "__main__":
    main()
