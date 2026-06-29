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
- Notifications: sends selection reports and confirmed trade alerts through email.

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
- `TurtleStrategyEngine`: buys immediately on a break above the previous 55-day high and sells
  immediately on a break below the previous 20-day low.
- `EmailNotificationService`: sends selection reports and trade signal email alerts.
- `AlertTradeGateway`: writes `orders/orders_YYYY-MM-DD.csv` and sends trade alerts.

## Run

Use `uv` to manage the local virtual environment and dependencies. Python 3.10 or newer is
recommended.

```powershell
uv sync
```

```powershell
uv run python main.py --once --dry-run
```

Dry run prints order intents without sending email or writing order CSV files.

For continuous intraday scanning every 60 seconds:

```powershell
uv run python main.py --loop --interval-seconds 60
```

For real alerts, copy `.env.example` to `.env`, update the SMTP values there, then run:

```powershell
uv run python main.py --once
```

The real `.env` file is ignored by git. Values in `.env` override the `email` section in
`config.json`; supported keys are `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
`SMTP_SENDER`, and comma-separated `SMTP_RECIPIENTS`.

## AKShare Data Source

Install the free market data dependency:

```powershell
uv sync
```

Export the latest A-share universe snapshot:

```powershell
uv run python akshare_sync.py --universe data/stock_universe.csv
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

## Position Sizing

Buy alerts include an ATR-based position-size suggestion. The default unit size is:

```text
shares = floor((account_equity * risk_per_trade / (ATR20 * stop_atr_multiple)) / lot_size) * lot_size
stop_loss = reference_price - stop_atr_multiple * ATR20
```

The defaults in `config.json` are `account_equity=1000000`, `risk_per_trade=0.01`,
`lot_size=100`, and `stop_atr_multiple=2.0`. Suggested size fields are written to order CSV files
and included in trade alert emails for manual review. With the default 2 ATR stop, the intended
maximum loss is about 1% of account equity before slippage and execution differences.

## Test

```powershell
uv run python -m unittest discover -s tests -v
```

The tests use mock data and fake email sending, so they do not require network access,
SMTP credentials, or a broker account.

## Weekly Offline Selection

Use offline data for the Sunday stock selection workflow. The selector reads local CSV files instead
of calling the market data API directly.

Initial download:

```powershell
uv run python sync_offline_data.py --init --lookback-days 500 --provider akshare --fallback eastmoney
```

If the market-cap snapshot is unstable, initialize code/name data first:

```powershell
uv run python sync_offline_data.py --init --lookback-days 500 --provider akshare --fallback none --skip-market-cap
```

For the first full local database build, prefer resumable batches after the universe file exists:

```powershell
uv run python sync_offline_data.py --init --lookback-days 500 --provider akshare --fallback none --skip-market-cap --skip-universe --skip-existing --batch-size 200 --workers 1 --request-delay 1 --max-retries 2 --akshare-timeout 30 --akshare-history-source auto
```

Repeat the same command until the number of `data/offline/daily_bars/*.csv` files is close to the
universe size. Failed symbols are appended to `data/offline/sync_failures.csv`, and existing daily
bar files are skipped when `--skip-existing` is used.

Refresh only the market-cap snapshot after daily bars have been downloaded:

```powershell
uv run python sync_offline_data.py --market-cap-only --provider eastmoney --fallback none --market-cap-fallback tencent --skip-existing --batch-size 500 --workers 1 --market-cap-page-size 100 --request-delay 0.2 --max-retries 2
```

Weekly Sunday update:

```powershell
.\scripts\weekly_update.ps1
```

Or run the Python entry directly:

```powershell
uv run python run_weekly_update.py --provider akshare --fallback eastmoney --akshare-history-source sina --lookback-days 14 --workers 1 --request-delay 0.5 --max-retries 2 --market-cap-provider eastmoney --market-cap-fallback tencent --market-cap-page-size 100
```

The weekly update reuses the existing offline universe, merges the latest daily bars by trade date,
refreshes market caps through the Eastmoney -> Tencent fallback chain, and writes the weekly
selection result to `selection_results/weekly_<date>.csv`.

To run the full service-style weekly selection manually, including refreshing `data/candidates.csv`
from the weekly selection result and current portfolio positions:

```powershell
uv run python run_weekly_selection.py
```

This manual entry defaults daily-bar sync to 4 process workers. To force serial mode for debugging:

```powershell
uv run python run_weekly_selection.py --bar-worker-mode serial
```

Add `--notify-selection` to send a plain-text email report after weekly selection. The report includes
the list before active turtle trend filtering, the final selected list, and the stocks excluded
because they are already in an active turtle trend. The command uses the same `config.json` / `.env`
SMTP settings as trade alerts.

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
- Strategy-triggered long/flat state is stored in `portfolio.json` separately from real share
  counts, so manual fills can still be maintained independently.
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
- not already in an active turtle trend: a close above the previous 55-day high starts a trend,
  and a later close below the previous 20-day low ends it
- total market cap > 20,000,000,000
