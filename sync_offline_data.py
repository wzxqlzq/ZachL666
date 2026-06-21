import argparse
import csv
from datetime import datetime
from pathlib import Path

from market_data import AkshareMarketDataProvider, EastmoneyMarketDataProvider, FallbackMarketDataProvider
from offline_data import OfflineDataStore, OfflineDataSync
from universe_provider import AkshareStockUniverseProvider, EastmoneyStockUniverseProvider, FallbackStockUniverseProvider


def build_market_data_provider(
    name: str,
    lookback_days: int,
    akshare_timeout: float | None = 30,
    akshare_history_source: str = "auto",
):
    if name == "akshare":
        return AkshareMarketDataProvider(
            lookback_days=lookback_days,
            adjust="qfq",
            timeout=akshare_timeout,
            history_source=akshare_history_source,
        )
    if name == "eastmoney":
        return EastmoneyMarketDataProvider(lookback_days=lookback_days, retries=3)
    raise ValueError(f"Unsupported market data provider: {name}")


def build_universe_provider(name: str, enrich_market_cap: bool):
    if name == "akshare":
        return AkshareStockUniverseProvider(enrich_market_cap=enrich_market_cap)
    if name == "eastmoney":
        return EastmoneyStockUniverseProvider()
    raise ValueError(f"Unsupported universe provider: {name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="data/offline")
    parser.add_argument("--init", action="store_true", help="download a full offline window")
    parser.add_argument("--incremental", action="store_true", help="download a short recent window")
    parser.add_argument("--lookback-days", type=int, default=None)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0, help="limit symbols for smoke tests")
    parser.add_argument("--batch-size", type=int, default=0, help="process only the next N missing symbols")
    parser.add_argument("--skip-existing", action="store_true", help="skip symbols with an existing daily CSV")
    parser.add_argument("--skip-universe", action="store_true", help="reuse existing offline stock_universe.csv")
    parser.add_argument("--failures-csv", default="data/offline/sync_failures.csv")
    parser.add_argument("--request-delay", type=float, default=0.0, help="seconds to wait before each symbol request")
    parser.add_argument("--max-retries", type=int, default=0, help="retry count per symbol after a failed request")
    parser.add_argument("--akshare-timeout", type=float, default=30, help="timeout seconds for AKShare HTTP calls")
    parser.add_argument(
        "--akshare-history-source",
        choices=["auto", "eastmoney", "sina"],
        default="auto",
        help="AKShare daily-bar source; auto tries stock_zh_a_hist then stock_zh_a_daily",
    )
    parser.add_argument("--provider", choices=["akshare", "eastmoney"], default="akshare")
    parser.add_argument("--fallback", choices=["none", "akshare", "eastmoney"], default="eastmoney")
    parser.add_argument(
        "--skip-market-cap",
        action="store_true",
        help="initialize code/name universe even if market cap snapshot is unavailable",
    )
    args = parser.parse_args()

    lookback_days = args.lookback_days
    if lookback_days is None:
        lookback_days = 500 if args.init else 14

    primary_market_data = build_market_data_provider(
        args.provider,
        lookback_days,
        akshare_timeout=args.akshare_timeout,
        akshare_history_source=args.akshare_history_source,
    )
    primary_universe = build_universe_provider(args.provider, enrich_market_cap=not args.skip_market_cap)
    fallback_market_data = None
    fallback_universe = None
    if args.fallback != "none" and args.fallback != args.provider:
        fallback_market_data = build_market_data_provider(
            args.fallback,
            lookback_days,
            akshare_timeout=args.akshare_timeout,
            akshare_history_source=args.akshare_history_source,
        )
        fallback_universe = build_universe_provider(args.fallback, enrich_market_cap=True)

    store = OfflineDataStore(args.root)
    sync = OfflineDataSync(
        store=store,
        universe_provider=FallbackStockUniverseProvider(primary_universe, fallback_universe),
        market_data_provider=FallbackMarketDataProvider(primary_market_data, fallback_market_data),
        workers=args.workers,
        request_delay_seconds=args.request_delay,
        max_retries=args.max_retries,
    )

    if args.skip_universe and store.universe_path.exists():
        universe_count = len(store.load_universe())
    else:
        universe_count = sync.sync_universe()

    limit = args.limit or args.batch_size
    success_count, failures = sync.sync_bars(
        limit=limit,
        skip_existing=args.skip_existing,
    )
    if failures:
        write_failures(args.failures_csv, failures)

    print(f"Universe rows: {universe_count}")
    print(f"Bars synced: {success_count}")
    print(f"Failures: {len(failures)}")
    print(f"Provider: {args.provider}")
    print(f"Fallback: {args.fallback}")
    if failures:
        print("Failure sample:")
        for symbol, error in failures[:10]:
            print(f"{symbol}: {error}")


def write_failures(path: str, failures: list[tuple[str, str]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    is_new = not target.exists()
    with target.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "symbol", "error"])
        if is_new:
            writer.writeheader()
        timestamp = datetime.now().isoformat(sep=" ", timespec="seconds")
        for symbol, error in failures:
            writer.writerow({"timestamp": timestamp, "symbol": symbol, "error": error})


if __name__ == "__main__":
    main()
