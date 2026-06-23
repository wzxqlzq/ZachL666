import argparse
import csv
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import date, datetime
from pathlib import Path

from market_data import FallbackMarketDataProvider
from models import Bar
from notifier import EmailNotificationService, SelectionReport, SmtpEmailSender
from offline_data import OfflineDataStore, OfflineDataSync, OfflineMarketDataProvider
from stock_selector import RuleBasedStockSelector
from sync_offline_data import build_market_data_provider, build_universe_provider, write_failures
from universe_provider import FallbackStockUniverseProvider


MIN_MARKET_CAP = 20_000_000_000


class TeeStream:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def run_weekly_update(args) -> tuple[int, int, int, int, Path]:
    store = OfflineDataStore(args.root)
    lookback_days = args.lookback_days

    primary_market_data = build_market_data_provider(
        args.provider,
        lookback_days,
        akshare_timeout=args.akshare_timeout,
        akshare_history_source=args.akshare_history_source,
    )
    fallback_market_data = None
    if args.fallback != "none" and args.fallback != args.provider:
        fallback_market_data = build_market_data_provider(
            args.fallback,
            lookback_days,
            akshare_timeout=args.akshare_timeout,
            akshare_history_source=args.akshare_history_source,
        )

    retries = args.max_retries + 1 if args.max_retries else 3
    primary_universe = build_universe_provider(
        args.market_cap_provider,
        enrich_market_cap=True,
        retries=retries,
        request_delay_seconds=args.request_delay,
        market_cap_page_size=args.market_cap_page_size,
    )
    fallback_universe = None
    if args.market_cap_fallback != "none" and args.market_cap_fallback != args.market_cap_provider:
        fallback_universe = build_universe_provider(
            args.market_cap_fallback,
            enrich_market_cap=True,
            retries=retries,
            request_delay_seconds=args.request_delay,
            market_cap_page_size=args.market_cap_page_size,
        )

    sync = OfflineDataSync(
        store=store,
        universe_provider=FallbackStockUniverseProvider(primary_universe, fallback_universe),
        market_data_provider=FallbackMarketDataProvider(primary_market_data, fallback_market_data),
        workers=args.workers,
        request_delay_seconds=args.request_delay,
        max_retries=args.max_retries,
        progress_callback=print,
    )

    if not store.universe_path.exists():
        sync.sync_universe()

    target_trade_date = _resolve_target_trade_date(args, store, primary_market_data)
    print(f"[target] trade_date={target_trade_date.isoformat()}")

    universe_rows = store.load_universe()
    update_symbols = _update_scope_symbols(universe_rows, args.update_scope)
    if args.limit:
        update_symbols = update_symbols[: args.limit]
    print(f"[scope] update_scope={args.update_scope} symbols={len(update_symbols)}")

    market_cap_count = 0
    market_cap_failures: list[tuple[str, str]] = []
    if not args.skip_market_cap_refresh:
        market_cap_symbols = _market_cap_stale_symbols(
            universe_rows,
            update_symbols,
            target_trade_date,
            force_refresh=args.force_market_cap_refresh,
        )
        print(
            "[market_caps] "
            f"scope={len(update_symbols)}, stale={len(market_cap_symbols)}, "
            f"force={args.force_market_cap_refresh}"
        )
        market_cap_count, market_cap_failures = sync.sync_market_caps(
            symbols=market_cap_symbols,
            skip_existing=args.skip_existing_market_cap,
        )
        if market_cap_failures:
            write_failures(
                args.failures_csv,
                market_cap_failures,
                stage="market_caps",
                context=_failure_context(args, stage_batch_size=args.market_cap_page_size),
            )

    universe_rows = store.load_universe()
    selection_symbols = _selection_scope_symbols(universe_rows)
    if args.limit:
        selection_symbols = selection_symbols[: args.limit]
    stale_symbols = _stale_symbols(store, selection_symbols, target_trade_date, args.skip_up_to_date_bars)
    print(
        "[bars] "
        f"selection_scope={len(selection_symbols)}, stale={len(stale_symbols)}, "
        f"skip_up_to_date={args.skip_up_to_date_bars}"
    )

    bar_count, bar_failures = _sync_weekly_bars(
        args=args,
        store=store,
        symbols=stale_symbols,
        target_trade_date=target_trade_date,
    )
    if bar_failures:
        write_failures(
            args.failures_csv,
            bar_failures,
            stage="bars",
            context=_failure_context(args, stage_batch_size=0),
        )

    selection_details = RuleBasedStockSelector(
        universe_csv_path=str(store.universe_path),
        market_data=OfflineMarketDataProvider(store),
    ).select_with_details(date.today())
    candidates = selection_details.selected

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

    if getattr(args, "notify_selection", False):
        notifier = build_notification_service()
        notifier.send_selection_report(
            SelectionReport(
                as_of=target_trade_date,
                output_path=output_path,
                before_trend_filter=selection_details.before_trend_filter,
                selected=selection_details.selected,
                excluded_by_active_trend=selection_details.excluded_by_active_trend,
            )
        )

    return bar_count, len(bar_failures), market_cap_count, len(market_cap_failures), output_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="data/offline")
    parser.add_argument("--output", default=f"selection_results/weekly_{date.today().isoformat()}.csv")
    parser.add_argument("--log-file", default="", help="write console output to this log file; defaults to logs/weekly_update_<timestamp>.log")
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="limit symbols for smoke tests")
    parser.add_argument("--failures-csv", default="data/offline/sync_failures.csv")
    parser.add_argument("--request-delay", type=float, default=0.5)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--provider", choices=["akshare", "eastmoney"], default="akshare")
    parser.add_argument("--fallback", choices=["none", "akshare", "eastmoney"], default="eastmoney")
    parser.add_argument("--akshare-timeout", type=float, default=30)
    parser.add_argument(
        "--akshare-history-source",
        choices=["auto", "eastmoney", "sina"],
        default="sina",
    )
    parser.add_argument("--market-cap-provider", choices=["eastmoney", "tencent"], default="eastmoney")
    parser.add_argument("--market-cap-fallback", choices=["none", "eastmoney", "tencent"], default="tencent")
    parser.add_argument("--market-cap-page-size", type=int, default=100)
    parser.add_argument("--force-market-cap-refresh", action="store_true")
    parser.add_argument("--target-date", default="auto", help="auto or YYYY-MM-DD")
    parser.add_argument("--target-probe-symbol", default="000001.SZ")
    parser.add_argument("--update-scope", choices=["selection", "full"], default="selection")
    parser.add_argument("--bar-worker-mode", choices=["process", "serial"], default="process")
    parser.add_argument("--bar-workers", type=int, default=4)
    parser.add_argument("--bar-batch-size", type=int, default=20)
    parser.add_argument("--bar-timeout-seconds", type=float, default=15)
    parser.add_argument("--final-retry-provider", choices=["none", "akshare", "eastmoney"], default="eastmoney")
    parser.add_argument("--skip-up-to-date-bars", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--skip-existing-market-cap",
        action="store_true",
        help="only fill missing/zero market caps instead of refreshing all market caps",
    )
    parser.add_argument("--skip-market-cap-refresh", action="store_true")
    parser.add_argument("--notify-selection", action="store_true", help="send the weekly selection report by email")
    args = parser.parse_args()

    log_path = Path(args.log_file) if args.log_file else _default_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    with log_path.open("w", encoding="utf-8", newline="") as log_file:
        sys.stdout = TeeStream(original_stdout, log_file)
        sys.stderr = TeeStream(original_stderr, log_file)
        try:
            print(f"[log] {log_path}")
            bar_count, bar_failure_count, market_cap_count, market_cap_failure_count, output_path = run_weekly_update(args)
            print(f"Bars synced: {bar_count}")
            print(f"Bar failures: {bar_failure_count}")
            print(f"Market caps synced: {market_cap_count}")
            print(f"Market cap failures: {market_cap_failure_count}")
            print(f"Output: {output_path}")
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr


def _default_log_path() -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return Path("logs") / f"weekly_update_{stamp}.log"


def build_notification_service() -> EmailNotificationService:
    from main import load_config

    config = load_config()
    return EmailNotificationService(SmtpEmailSender(config["email"]))


def _failure_context(args, stage_batch_size: int = 0) -> dict[str, object]:
    return {
        "provider": args.provider,
        "fallback": args.fallback,
        "market_cap_fallback": args.market_cap_fallback,
        "workers": getattr(args, "bar_workers", args.workers),
        "batch_size": stage_batch_size,
        "limit": args.limit,
        "skip_existing": False,
    }


def _resolve_target_trade_date(args, store: OfflineDataStore, market_data_provider) -> date:
    if args.target_date != "auto":
        return date.fromisoformat(args.target_date)
    local_date = _local_target_trade_date(store)
    if local_date is not None:
        return local_date
    bars = market_data_provider.load_daily_bars(args.target_probe_symbol, end_date=date.today())
    if not bars:
        raise RuntimeError(f"No bars returned for target probe symbol {args.target_probe_symbol}")
    return bars[-1].trade_date


def _local_target_trade_date(store: OfflineDataStore) -> date | None:
    dates = Counter()
    if not store.daily_dir.exists():
        return None
    for path in store.daily_dir.glob("*.csv"):
        try:
            last_line = _last_csv_data_line(path)
        except Exception:
            continue
        if last_line:
            dates[date.fromisoformat(last_line.split(",", 1)[0].lstrip("\ufeff"))] += 1
    if not dates:
        return None
    return dates.most_common(1)[0][0]


def _last_csv_data_line(path: Path) -> str:
    last_line = ""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                last_line = stripped
    if last_line.startswith("date,"):
        return ""
    return last_line


def _update_scope_symbols(rows: list[dict[str, str]], update_scope: str) -> list[str]:
    symbols = []
    for row in rows:
        symbol = row.get("symbol", "")
        if not symbol:
            continue
        if update_scope == "selection" and (_is_excluded(row) or _is_beijing(row)):
            continue
        symbols.append(symbol)
    return symbols


def _selection_scope_symbols(rows: list[dict[str, str]]) -> list[str]:
    symbols = []
    for row in rows:
        symbol = row.get("symbol", "")
        if not symbol or _is_excluded(row) or _is_beijing(row):
            continue
        if _market_cap(row) <= MIN_MARKET_CAP:
            continue
        symbols.append(symbol)
    return symbols


def _stale_symbols(
    store: OfflineDataStore,
    symbols: list[str],
    target_trade_date: date,
    skip_up_to_date: bool,
) -> list[str]:
    if not skip_up_to_date:
        return symbols
    stale = []
    for symbol in symbols:
        latest = store.latest_bar_date(symbol)
        if latest != target_trade_date:
            stale.append(symbol)
    return stale


def _market_cap_stale_symbols(
    rows: list[dict[str, str]],
    symbols: list[str],
    target_trade_date: date,
    force_refresh: bool,
) -> list[str]:
    if force_refresh:
        return symbols
    rows_by_symbol = {row.get("symbol", ""): row for row in rows}
    stale = []
    for symbol in symbols:
        row = rows_by_symbol.get(symbol, {})
        if row.get("updated_at") == target_trade_date.isoformat() and _market_cap(row) > 0:
            continue
        stale.append(symbol)
    return stale


def _sync_weekly_bars(
    args,
    store: OfflineDataStore,
    symbols: list[str],
    target_trade_date: date,
) -> tuple[int, list[tuple[str, str]]]:
    if not symbols:
        print("[bars] nothing to update")
        return 0, []
    if args.bar_worker_mode == "serial":
        return _sync_bars_serial(args, store, symbols, target_trade_date, stage="bars_serial")

    success_count, failures = _sync_bars_process(args, store, symbols, target_trade_date)
    if failures:
        timeout_failures = [(symbol, error) for symbol, error in failures if "timed out" in error]
        retry_symbols = [symbol for symbol, error in failures if "timed out" not in error]
        retry_failures: list[tuple[str, str]] = []
        if retry_symbols:
            print(f"[bars] process stage failures={len(retry_symbols)}, retrying serial")
            retry_success, retry_failures = _sync_bars_serial(
                args,
                store,
                retry_symbols,
                target_trade_date,
                stage="bars_serial_retry",
            )
            success_count += retry_success
        failures = timeout_failures + retry_failures
    if failures and args.final_retry_provider != "none":
        retry_symbols = [symbol for symbol, _error in failures]
        print(f"[bars] final provider retry={args.final_retry_provider} symbols={len(retry_symbols)}")
        retry_success, retry_failures = _sync_bars_serial(
            args,
            store,
            retry_symbols,
            target_trade_date,
            stage="bars_final_retry",
            provider_name=args.final_retry_provider,
            fallback_name="none",
        )
        success_count += retry_success
        failures = retry_failures
    return success_count, failures


def _sync_bars_process(
    args,
    store: OfflineDataStore,
    symbols: list[str],
    target_trade_date: date,
) -> tuple[int, list[tuple[str, str]]]:
    batches = _batches(symbols, max(1, args.bar_batch_size))
    success_count = 0
    failures: list[tuple[str, str]] = []
    worker_count = min(max(1, args.bar_workers), max(1, len(batches)))
    print(
        "[bars_process] "
        f"start symbols={len(symbols)} batches={len(batches)} workers={worker_count} batch_size={args.bar_batch_size}"
    )
    params = _bar_worker_params(args, target_trade_date)
    executor = ProcessPoolExecutor(max_workers=worker_count)
    future_to_batch = {executor.submit(_fetch_bar_batch, batch, params): batch for batch in batches}
    pending = set(future_to_batch)
    completed = 0
    timed_out = False
    try:
        while pending:
            done, pending = wait(pending, timeout=args.bar_timeout_seconds, return_when=FIRST_COMPLETED)
            if not done:
                timed_out = True
                for future in pending:
                    batch = future_to_batch[future]
                    future.cancel()
                    failures.extend((symbol, f"process batch timed out after {args.bar_timeout_seconds}s") for symbol in batch)
                print(
                    "[bars_process] "
                    f"timeout pending_batches={len(pending)}, success={success_count}, failures={len(failures)}"
                )
                break
            for future in done:
                completed += 1
                batch = future_to_batch[future]
                try:
                    fetched, batch_failures = future.result()
                except Exception as exc:
                    failures.extend((symbol, f"process batch failed: {exc}") for symbol in batch)
                    fetched = []
                    batch_failures = []
                for symbol, bars in fetched:
                    bar_error = _bar_target_error(bars, target_trade_date)
                    if bar_error:
                        failures.append((symbol, bar_error))
                        continue
                    store.save_bars(symbol, bars)
                    success_count += 1
                failures.extend(batch_failures)
                print(
                    "[bars_process] "
                    f"batches={completed}/{len(batches)}, success={success_count}, failures={len(failures)}"
                )
    finally:
        processes = list(getattr(executor, "_processes", {}).values())
        executor.shutdown(wait=False, cancel_futures=True)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
    return success_count, failures


def _sync_bars_serial(
    args,
    store: OfflineDataStore,
    symbols: list[str],
    target_trade_date: date,
    stage: str,
    provider_name: str | None = None,
    fallback_name: str | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    provider_name = provider_name or args.provider
    fallback_name = fallback_name if fallback_name is not None else args.fallback
    provider = build_market_data_provider(
        provider_name,
        args.lookback_days,
        akshare_timeout=args.akshare_timeout,
        akshare_history_source=args.akshare_history_source,
    )
    fallback = None
    if fallback_name != "none" and fallback_name != provider_name:
        fallback = build_market_data_provider(
            fallback_name,
            args.lookback_days,
            akshare_timeout=args.akshare_timeout,
            akshare_history_source=args.akshare_history_source,
        )
    market_data = FallbackMarketDataProvider(provider, fallback)
    success_count = 0
    failures: list[tuple[str, str]] = []
    print(f"[{stage}] start symbols={len(symbols)} provider={provider_name} fallback={fallback_name}")
    for index, symbol in enumerate(symbols, start=1):
        try:
            bars = market_data.load_daily_bars(symbol, end_date=target_trade_date)
            bar_error = _bar_target_error(bars, target_trade_date)
            if bar_error:
                raise RuntimeError(bar_error)
            store.save_bars(symbol, bars)
            success_count += 1
        except Exception as exc:
            failures.append((symbol, str(exc)))
        if args.request_delay:
            time.sleep(args.request_delay)
        print(f"[{stage}] {index}/{len(symbols)} success={success_count} failures={len(failures)} latest={symbol}")
    return success_count, failures


def _fetch_bar_batch(symbols: list[str], params: dict) -> tuple[list[tuple[str, list[Bar]]], list[tuple[str, str]]]:
    provider = build_market_data_provider(
        params["provider"],
        params["lookback_days"],
        akshare_timeout=params["akshare_timeout"],
        akshare_history_source=params["akshare_history_source"],
    )
    fallback = None
    if params["fallback"] != "none" and params["fallback"] != params["provider"]:
        fallback = build_market_data_provider(
            params["fallback"],
            params["lookback_days"],
            akshare_timeout=params["akshare_timeout"],
            akshare_history_source=params["akshare_history_source"],
        )
    market_data = FallbackMarketDataProvider(provider, fallback)
    target_trade_date = date.fromisoformat(params["target_trade_date"])
    fetched: list[tuple[str, list[Bar]]] = []
    failures: list[tuple[str, str]] = []
    for symbol in symbols:
        try:
            bars = market_data.load_daily_bars(symbol, end_date=target_trade_date)
            fetched.append((symbol, bars))
        except Exception as exc:
            failures.append((symbol, str(exc)))
        if params["request_delay"]:
            time.sleep(params["request_delay"])
    return fetched, failures


def _bar_target_error(bars: list[Bar], target_trade_date: date) -> str:
    if not bars:
        return "no bars returned"
    latest = max(bar.trade_date for bar in bars)
    if latest < target_trade_date:
        return f"latest bar {latest.isoformat()} before target {target_trade_date.isoformat()}"
    return ""


def _bar_worker_params(args, target_trade_date: date) -> dict:
    return {
        "provider": args.provider,
        "fallback": args.fallback,
        "lookback_days": args.lookback_days,
        "akshare_timeout": args.akshare_timeout,
        "akshare_history_source": args.akshare_history_source,
        "target_trade_date": target_trade_date.isoformat(),
        "request_delay": args.request_delay,
    }


def _batches(symbols: list[str], batch_size: int) -> list[list[str]]:
    return [symbols[index : index + batch_size] for index in range(0, len(symbols), batch_size)]


def _is_excluded(row: dict[str, str]) -> bool:
    name = row.get("name", "")
    status = row.get("status", "").strip().lower()
    return "ST" in name.upper() or status in {"suspended", "delisted", "exclude"}


def _is_beijing(row: dict[str, str]) -> bool:
    symbol = row.get("symbol", "")
    exchange = row.get("exchange", "").upper()
    return symbol.endswith(".BJ") or exchange in {"BJ", "BSE", "BEIJING"}


def _market_cap(row: dict[str, str]) -> float:
    raw = row.get("market_cap") or row.get("total_market_cap") or "0"
    return float(str(raw).replace(",", ""))


if __name__ == "__main__":
    main()
