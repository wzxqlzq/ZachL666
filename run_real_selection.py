import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import requests

from market_data import EastmoneyMarketDataProvider
from stock_selector import RuleBasedStockSelector


class MemoryMarketDataProvider:
    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol

    def load_daily_bars(self, symbol: str, end_date: date | None = None):
        bars = self.bars_by_symbol.get(symbol, [])
        if end_date:
            return [bar for bar in bars if bar.trade_date <= end_date]
        return bars

    def get_quote(self, symbol: str, at=None):
        raise NotImplementedError


def fetch_universe() -> list[dict[str, str]]:
    url = "https://82.push2.eastmoney.com/api/qt/clist/get"
    fields = "f12,f13,f14,f20"
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
    headers = {"User-Agent": "Mozilla/5.0"}
    rows: list[dict[str, str]] = []
    page = 1
    page_size = 200

    while True:
        params = {
            "pn": page,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f12",
            "fs": fs,
            "fields": fields,
        }
        response = requests.get(url, params=params, timeout=20, headers=headers)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        diff = data.get("diff") or []
        if not diff:
            break

        for item in diff:
            code = str(item.get("f12") or "")
            rows.append(
                {
                    "symbol": symbol_with_suffix(code, item.get("f13")),
                    "name": str(item.get("f14") or ""),
                    "exchange": exchange_from_market(item.get("f13")),
                    "market_cap": str(item.get("f20") or 0),
                    "status": "",
                }
            )

        if len(rows) >= int(data.get("total") or 0):
            break
        page += 1

    return rows


def symbol_with_suffix(code: str, market) -> str:
    exchange = exchange_from_market(market)
    return f"{code}.{exchange}" if exchange else code


def exchange_from_market(market) -> str:
    if int(market or 0) == 1:
        return "SH"
    if int(market or 0) == 0:
        return "SZ"
    return ""


def write_universe(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "name", "exchange", "market_cap", "status"])
        writer.writeheader()
        writer.writerows(rows)


def read_universe(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def static_prefilter(rows: list[dict[str, str]], min_market_cap: float) -> list[dict[str, str]]:
    result = []
    for row in rows:
        name = row.get("name") or ""
        symbol = row.get("symbol") or ""
        exchange = row.get("exchange") or ""
        market_cap = float(row.get("market_cap") or 0)
        if "ST" in name.upper():
            continue
        if symbol.endswith(".BJ") or exchange == "BJ":
            continue
        if market_cap <= min_market_cap:
            continue
        result.append(row)
    return result


def fetch_bars(symbol: str, lookback_days: int):
    provider = EastmoneyMarketDataProvider(lookback_days=lookback_days, retries=3, retry_sleep_seconds=0.5)
    return symbol, provider.load_daily_bars(symbol, end_date=date.today())


def write_candidates(path: Path, candidates) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "name", "reason"])
        writer.writeheader()
        for item in candidates:
            writer.writerow({"symbol": item.symbol, "name": item.name, "reason": item.reason})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=420)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0, help="limit static-filtered universe for smoke tests")
    parser.add_argument("--universe", default="data/stock_universe_real.csv")
    parser.add_argument("--output", default=f"selection_results/selected_{date.today().isoformat()}.csv")
    args = parser.parse_args()

    universe_path = Path(args.universe)
    output_path = Path(args.output)

    rows = fetch_universe()
    write_universe(universe_path, rows)
    filtered = static_prefilter(rows, min_market_cap=20_000_000_000)
    if args.limit:
        filtered = filtered[: args.limit]

    print(f"Universe: {len(rows)}")
    print(f"After static filters: {len(filtered)}")

    bars_by_symbol = {}
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(fetch_bars, row["symbol"], args.lookback_days): row["symbol"] for row in filtered}
        for index, future in enumerate(as_completed(futures), start=1):
            symbol = futures[future]
            try:
                fetched_symbol, bars = future.result()
                bars_by_symbol[fetched_symbol] = bars
            except Exception as exc:
                failures.append((symbol, str(exc)))
            if index % 50 == 0 or index == len(futures):
                print(f"Fetched bars: {index}/{len(futures)}, failures: {len(failures)}")

    selector = RuleBasedStockSelector(
        universe_csv_path=str(universe_path),
        market_data=MemoryMarketDataProvider(bars_by_symbol),
    )
    candidates = selector.select(date.today())
    if args.limit:
        candidate_symbols = {row["symbol"] for row in filtered}
        candidates = [item for item in candidates if item.symbol in candidate_symbols]

    write_candidates(output_path, candidates)
    print(f"Selected: {len(candidates)}")
    print(f"Output: {output_path}")
    if failures:
        print("Failures sample:")
        for symbol, error in failures[:10]:
            print(f"{symbol}: {error}")


if __name__ == "__main__":
    main()
