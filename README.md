# A-share Turtle Alert Framework

This project is a semi-automatic trading alert framework for A-share turtle-style strategies.
It does not connect to a broker and does not place orders. The first trade gateway only sends
email alerts and records order intent CSV files for manual review.

## Architecture

The project is split into four replaceable modules:

- Stock selection: outputs `StockCandidate` objects.
- Market data: outputs normalized `Bar` and `Quote` objects.
- Strategy trigger: turns candidates, bars, quotes, and positions into `Signal` objects.
- Trade gateway: turns confirmed signals into `OrderIntent` records and notifications.

`Runner` is the only orchestration layer. It wires modules together but does not contain stock
selection, market data, strategy, or trading logic.

## Module Contracts

The shared data objects live in `models.py`:

- `Bar`
- `Quote`
- `StockCandidate`
- `Position`
- `Signal`
- `OrderIntent`

The module protocols live in `interfaces.py`:

- `StockSelector`
- `MarketDataProvider`
- `StrategyEngine`
- `TradeGateway`

Each module should depend on these contracts instead of another module's concrete implementation.

## Current Implementations

- `CsvStockSelector`: reads the candidate pool from `data/candidates.csv`.
- `RuleBasedStockSelector`: applies the A-share universe rules from `data/stock_universe.csv`.
- `CsvMarketDataProvider`: reads daily bars from `data/sample_daily.csv` and can return a mock quote.
- `AkshareMarketDataProvider`: reads A-share daily bars and spot quotes from AKShare.
- `TurtleStrategyEngine`: implements turtle breakout/exit logic with a 5-minute confirmation delay.
- `AlertTradeGateway`: sends email and writes `orders/orders_YYYY-MM-DD.csv`.

## Run

```powershell
cd C:\Users\wzxql\Documents\Codex\2026-06-20\new-chat-2\outputs\a_share_turtle_alert
python main.py --once --dry-run
```

Dry run prints order intents without sending email or writing order CSV files.

For real alerts, update `config.json` with SMTP settings, then run:

```powershell
python main.py --once
```

## AKShare Data Source

Install the free market data dependency:

```powershell
python -m pip install -r requirements.txt
```

Export the latest A-share universe snapshot:

```powershell
python akshare_sync.py --universe data/stock_universe.csv
```

To use AKShare directly for daily bars and spot quotes, set `config.json`:

```json
{
  "market_data": {
    "provider": "akshare",
    "lookback_days": 220,
    "adjust": "qfq"
  }
}
```

`stock_zh_a_hist` is used first for historical daily bars. If it fails, the provider can fall back
to AKShare's `stock_zh_a_daily` Sina source. `stock_zh_a_spot_em` is used for spot quotes and stock
universe export.

## Test

```powershell
python -m unittest discover -s tests -v
```

The tests use mock data and fake email sending, so they do not require network access,
SMTP credentials, or a broker account.

## Weekly Offline Selection

Use offline data for the Sunday stock selection workflow. The selector reads local CSV files instead
of calling the market data API directly.

Initial download:

```powershell
python sync_offline_data.py --init --lookback-days 500 --provider akshare --fallback eastmoney
```

If the market-cap snapshot is unstable, initialize code/name data first:

```powershell
python sync_offline_data.py --init --lookback-days 500 --provider akshare --fallback none --skip-market-cap
```

For the first full local database build, prefer resumable batches after the universe file exists:

```powershell
python sync_offline_data.py --init --lookback-days 500 --provider akshare --fallback none --skip-market-cap --skip-universe --skip-existing --batch-size 200 --workers 1 --request-delay 1 --max-retries 2 --akshare-timeout 30 --akshare-history-source auto
```

Repeat the same command until the number of `data/offline/daily_bars/*.csv` files is close to the
universe size. Failed symbols are appended to `data/offline/sync_failures.csv`, and existing daily
bar files are skipped when `--skip-existing` is used.

Refresh only the market-cap snapshot after daily bars have been downloaded:

```powershell
python sync_offline_data.py --market-cap-only --provider eastmoney --fallback none --market-cap-fallback tencent --skip-existing --batch-size 500 --workers 1 --market-cap-page-size 100 --request-delay 0.2 --max-retries 2
```

Weekly Sunday update:

```powershell
.\scripts\weekly_update.ps1
```

Or run the Python entry directly:

```powershell
python run_weekly_update.py --provider akshare --fallback eastmoney --akshare-history-source sina --lookback-days 14 --workers 1 --request-delay 0.5 --max-retries 2 --market-cap-provider eastmoney --market-cap-fallback tencent --market-cap-page-size 100
```

The weekly update reuses the existing offline universe, merges the latest daily bars by trade date,
refreshes market caps through the Eastmoney -> Tencent fallback chain, and writes the weekly
selection result to `selection_results/weekly_<date>.csv`.

Offline files are stored under:

```text
data/offline/stock_universe.csv
data/offline/daily_bars/<symbol>.csv
```

The daily bar files are merged by trade date, so incremental updates can safely overlap existing
data. Sunday has no A-share daily bar; the update fetches data through the most recent trading day.

`sync_offline_data.py` depends on provider interfaces. The default provider is AKShare; Eastmoney is
only a fallback when explicitly configured.

## Next Integration Points

- Feed `RuleBasedStockSelector` with a real stock universe file containing `symbol`, `name`,
  `exchange`, `market_cap`, and `status`.
- Feed daily bars with `amount` when available. If `amount` is missing, the selector falls back
  to `close * volume`, which is only a rough substitute for turnover.
- Add a local cache if AKShare rate limits or network instability becomes an issue.
- Keep `TurtleStrategyEngine` isolated from external APIs.
- Add future broker or GUI gateways by implementing `TradeGateway`; do not modify strategy code.

## Rule-Based Stock Selection

The implemented selector matches these rules:

- close > MA50
- MA50 > MA120
- 20-day average turnover amount > 500,000,000
- 20-day ATR / close is between 3% and 7%
- non-ST
- non-Beijing Stock Exchange
- 20-day lowest low > MA120
- 10-day lowest low > MA50
- close < previous 55-day highest high
- total market cap > 20,000,000,000
