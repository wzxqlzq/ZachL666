import csv
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Protocol

from models import Bar, Quote


class StockUniverseProvider(Protocol):
    def load_universe(self) -> list[dict[str, str]]:
        ...


class OfflineDataStore:
    def __init__(self, root: str = "data/offline"):
        self.root = Path(root)
        self.daily_dir = self.root / "daily_bars"
        self.universe_path = self.root / "stock_universe.csv"

    def save_universe(self, rows: list[dict[str, str]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fieldnames = ["symbol", "name", "exchange", "market_cap", "status", "updated_at"]
        today = date.today().isoformat()
        normalized = []
        for row in rows:
            normalized.append(
                {
                    "symbol": row.get("symbol", ""),
                    "name": row.get("name", ""),
                    "exchange": row.get("exchange", ""),
                    "market_cap": row.get("market_cap", "0"),
                    "status": row.get("status", ""),
                    "updated_at": row.get("updated_at") or today,
                }
            )
        with self.universe_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(normalized)

    def load_universe(self) -> list[dict[str, str]]:
        if not self.universe_path.exists():
            return []
        with self.universe_path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def update_universe_market_caps(self, rows: list[dict[str, str]]) -> int:
        current_rows = self.load_universe()
        if not current_rows:
            self.save_universe(rows)
            return sum(1 for row in rows if self._market_cap_value(row) > 0)

        updates = {row.get("symbol", ""): row for row in rows if row.get("symbol")}
        updated_count = 0
        for current in current_rows:
            symbol = current.get("symbol", "")
            update = updates.get(symbol)
            if not update:
                continue
            market_cap = self._market_cap_value(update)
            if market_cap > 0:
                current["market_cap"] = str(int(market_cap))
                updated_count += 1
            for field in ["name", "exchange", "status"]:
                if not current.get(field) and update.get(field):
                    current[field] = update[field]
            current["updated_at"] = date.today().isoformat()

        self.save_universe(current_rows)
        return updated_count

    def _market_cap_value(self, row: dict[str, str]) -> float:
        raw = row.get("market_cap") or row.get("total_market_cap") or "0"
        return float(str(raw).replace(",", ""))

    def save_bars(self, symbol: str, bars: list[Bar]) -> None:
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        merged = {bar.trade_date: bar for bar in self.load_bars(symbol)}
        for bar in bars:
            merged[bar.trade_date] = bar
        path = self._bar_path(symbol)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["date", "symbol", "open", "high", "low", "close", "volume", "amount"],
            )
            writer.writeheader()
            for bar in sorted(merged.values(), key=lambda item: item.trade_date):
                writer.writerow(
                    {
                        "date": bar.trade_date.isoformat(),
                        "symbol": bar.symbol,
                        "open": f"{bar.open:.4f}",
                        "high": f"{bar.high:.4f}",
                        "low": f"{bar.low:.4f}",
                        "close": f"{bar.close:.4f}",
                        "volume": bar.volume,
                        "amount": "" if bar.amount is None else f"{bar.amount:.2f}",
                    }
                )

    def load_bars(self, symbol: str, end_date: date | None = None) -> list[Bar]:
        path = self._bar_path(symbol)
        if not path.exists():
            return []
        bars: list[Bar] = []
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                trade_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
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

    def latest_bar_date(self, symbol: str) -> date | None:
        bars = self.load_bars(symbol)
        return bars[-1].trade_date if bars else None

    def has_bars(self, symbol: str) -> bool:
        return self._bar_path(symbol).exists()

    def _bar_path(self, symbol: str) -> Path:
        return self.daily_dir / f"{symbol}.csv"


class OfflineMarketDataProvider:
    def __init__(self, store: OfflineDataStore):
        self.store = store

    def load_daily_bars(self, symbol: str, end_date: date | None = None) -> list[Bar]:
        return self.store.load_bars(symbol, end_date=end_date)

    def get_quote(self, symbol: str, at: datetime | None = None) -> Quote:
        bars = self.store.load_bars(symbol)
        if not bars:
            raise ValueError(f"No offline bars found for {symbol}")
        latest = bars[-1]
        return Quote(
            symbol=symbol,
            timestamp=at or datetime.combine(latest.trade_date, datetime.min.time()),
            price=latest.close,
            open=latest.open,
            high=latest.high,
            low=latest.low,
            volume=latest.volume,
        )


class OfflineDataSync:
    def __init__(
        self,
        store: OfflineDataStore,
        universe_provider: StockUniverseProvider,
        market_data_provider,
        workers: int = 6,
        request_delay_seconds: float = 0.0,
        max_retries: int = 0,
        progress_callback: Callable[[str], None] | None = None,
        progress_interval: int = 50,
    ):
        self.store = store
        self.universe_provider = universe_provider
        self.market_data_provider = market_data_provider
        self.workers = workers
        self.request_delay_seconds = request_delay_seconds
        self.max_retries = max_retries
        self.progress_callback = progress_callback
        self.progress_interval = max(1, progress_interval)

    def _progress(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(message)

    def sync_universe(self) -> int:
        self._progress("[universe] loading universe")
        rows = self.universe_provider.load_universe()
        self.store.save_universe(rows)
        self._progress(f"[universe] saved rows={len(rows)}")
        return len(rows)

    def sync_market_caps(
        self,
        symbols: list[str] | None = None,
        limit: int = 0,
        skip_existing: bool = False,
    ) -> tuple[int, list[tuple[str, str]]]:
        rows = self.store.load_universe()
        if rows and hasattr(self.universe_provider, "load_market_cap"):
            selected_rows = rows
            if skip_existing:
                selected_rows = [row for row in selected_rows if self.store._market_cap_value(row) <= 0]
            selected_symbols = symbols if symbols is not None else [row["symbol"] for row in selected_rows if row.get("symbol")]
            if limit:
                selected_symbols = selected_symbols[:limit]
            if hasattr(self.universe_provider, "load_market_caps"):
                return self._sync_market_cap_batches(selected_symbols)
            failures: list[tuple[str, str]] = []
            market_cap_rows: list[dict[str, str]] = []
            total = len(selected_symbols)
            self._progress(f"[market_caps] start symbols={total} workers={self.workers}")
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = {
                    executor.submit(self._load_market_cap_with_retry, symbol): symbol
                    for symbol in selected_symbols
                }
                for completed, future in enumerate(as_completed(futures), start=1):
                    symbol = futures[future]
                    try:
                        market_cap_rows.append(future.result())
                    except Exception as exc:
                        failures.append((symbol, str(exc)))
                    if completed % self.progress_interval == 0 or completed == total:
                        self._progress(
                            "[market_caps] "
                            f"{completed}/{total} done, rows={len(market_cap_rows)}, failures={len(failures)}, latest={symbol}"
                        )
            updated_count = self.store.update_universe_market_caps(market_cap_rows)
            self._progress(f"[market_caps] saved rows={updated_count}, failures={len(failures)}")
            return updated_count, failures

        market_cap_rows = self.universe_provider.load_universe()
        updated_count = self.store.update_universe_market_caps(market_cap_rows)
        self._progress(f"[market_caps] saved rows={updated_count}")
        return updated_count, []

    def _sync_market_cap_batches(self, symbols: list[str]) -> tuple[int, list[tuple[str, str]]]:
        failures: list[tuple[str, str]] = []
        market_cap_rows: list[dict[str, str]] = []
        batch_size = max(1, int(getattr(self.universe_provider, "page_size", 20)))
        batches = [symbols[index : index + batch_size] for index in range(0, len(symbols), batch_size)]
        total_batches = len(batches)
        total_symbols = len(symbols)
        self._progress(
            f"[market_caps] start symbols={total_symbols} batches={total_batches} batch_size={batch_size} workers={self.workers}"
        )
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {
                executor.submit(self._load_market_caps_with_retry, batch): batch
                for batch in batches
            }
            for completed_batches, future in enumerate(as_completed(futures), start=1):
                batch = futures[future]
                try:
                    market_cap_rows.extend(future.result())
                except Exception as exc:
                    failures.extend((symbol, str(exc)) for symbol in batch)
                completed_symbols = min(completed_batches * batch_size, total_symbols)
                if completed_batches % self.progress_interval == 0 or completed_batches == total_batches:
                    self._progress(
                        "[market_caps] "
                        f"batches={completed_batches}/{total_batches}, symbols~={completed_symbols}/{total_symbols}, "
                        f"rows={len(market_cap_rows)}, failures={len(failures)}"
                    )
        updated_count = self.store.update_universe_market_caps(market_cap_rows)
        self._progress(f"[market_caps] saved rows={updated_count}, failures={len(failures)}")
        return updated_count, failures

    def sync_bars(
        self,
        symbols: list[str] | None = None,
        limit: int = 0,
        skip_existing: bool = False,
    ) -> tuple[int, list[tuple[str, str]]]:
        rows = self.store.load_universe()
        selected_symbols = symbols if symbols is not None else [row["symbol"] for row in rows if row.get("symbol")]
        if skip_existing:
            selected_symbols = [symbol for symbol in selected_symbols if not self.store.has_bars(symbol)]
        if limit:
            selected_symbols = selected_symbols[:limit]

        failures: list[tuple[str, str]] = []
        success_count = 0
        total = len(selected_symbols)
        self._progress(f"[bars] start symbols={total} workers={self.workers}")
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {
                executor.submit(self._load_daily_bars_with_retry, symbol): symbol
                for symbol in selected_symbols
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                symbol = futures[future]
                try:
                    bars = future.result()
                    if bars:
                        self.store.save_bars(symbol, bars)
                    success_count += 1
                except Exception as exc:
                    failures.append((symbol, str(exc)))
                if completed % self.progress_interval == 0 or completed == total:
                    self._progress(
                        f"[bars] {completed}/{total} done, success={success_count}, failures={len(failures)}, latest={symbol}"
                    )
        self._progress(f"[bars] saved symbols={success_count}, failures={len(failures)}")
        return success_count, failures

    def _load_daily_bars_with_retry(self, symbol: str) -> list[Bar]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if self.request_delay_seconds and attempt == 0:
                time.sleep(self.request_delay_seconds)
            try:
                return self.market_data_provider.load_daily_bars(symbol)
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self.request_delay_seconds or 1.0)
        raise last_error or RuntimeError(f"Failed to load bars for {symbol}")

    def _load_market_cap_with_retry(self, symbol: str) -> dict[str, str]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if self.request_delay_seconds and attempt == 0:
                time.sleep(self.request_delay_seconds)
            try:
                return self.universe_provider.load_market_cap(symbol)
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self.request_delay_seconds or 1.0)
        raise last_error or RuntimeError(f"Failed to load market cap for {symbol}")

    def _load_market_caps_with_retry(self, symbols: list[str]) -> list[dict[str, str]]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if self.request_delay_seconds and attempt == 0:
                time.sleep(self.request_delay_seconds)
            try:
                return self.universe_provider.load_market_caps(symbols)
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self.request_delay_seconds or 1.0)
        raise last_error or RuntimeError(f"Failed to load market caps for {symbols[0]}...")
