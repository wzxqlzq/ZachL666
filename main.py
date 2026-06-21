import argparse
import json
import logging
import shutil
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from market_data import AkshareMarketDataProvider, CsvMarketDataProvider, EastmoneyMarketDataProvider
from notifier import EmailNotifier, SmtpEmailSender
from portfolio import PortfolioRepository
from runner import Runner
from stock_selector import CsvStockSelector, RuleBasedStockSelector
from signal_store import SignalStore
from strategy import TurtleStrategyEngine
from trade_gateway import AlertTradeGateway


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def ensure_local_files() -> None:
    if not Path("config.json").exists():
        shutil.copyfile("config.example.json", "config.json")
    if not Path("portfolio.json").exists():
        shutil.copyfile("portfolio.example.json", "portfolio.json")


def load_config() -> dict:
    defaults = load_json("config.example.json")
    local = load_json("config.json") if Path("config.json").exists() else {}
    return deep_merge(defaults, local)


def setup_logging(log_file: str) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def build_market_data(config: dict):
    provider = config["market_data"].get("provider", "csv")
    if provider == "csv":
        return CsvMarketDataProvider(config["market_data"]["daily_csv_path"])
    if provider == "akshare":
        return AkshareMarketDataProvider(
            lookback_days=config["market_data"].get("lookback_days", 220),
            adjust=config["market_data"].get("adjust", "qfq"),
        )
    if provider == "eastmoney":
        return EastmoneyMarketDataProvider(
            lookback_days=config["market_data"].get("lookback_days", 220),
            adjust=config["market_data"].get("adjust", "qfq"),
        )
    raise ValueError(f"Unsupported market data provider: {provider}")


def build_selector(config: dict, market_data):
    provider = config["selector"].get("provider", "csv")
    if provider == "csv":
        return CsvStockSelector(config["selector"]["csv_path"])
    if provider == "rule_based":
        return RuleBasedStockSelector(
            universe_csv_path=config["selector"]["universe_csv_path"],
            market_data=market_data,
            min_avg_amount_20=config["selector"].get("min_avg_amount_20", 500_000_000),
            min_atr_ratio=config["selector"].get("min_atr_ratio", 0.03),
            max_atr_ratio=config["selector"].get("max_atr_ratio", 0.07),
            min_market_cap=config["selector"].get("min_market_cap", 20_000_000_000),
        )
    raise ValueError(f"Unsupported selector provider: {provider}")


def run_once(dry_run: bool) -> None:
    ensure_local_files()
    config = load_config()
    setup_logging(config["paths"]["log_file"])

    market_data = build_market_data(config)
    selector = build_selector(config, market_data)
    strategy = TurtleStrategyEngine(**config["strategy"])
    positions = PortfolioRepository(config["paths"]["portfolio"]).load_positions()
    notifier = EmailNotifier(SmtpEmailSender(config["email"]))
    gateway = AlertTradeGateway(
        notifier=notifier,
        signal_store=SignalStore(config["paths"]["signal_db"]),
        orders_dir=config["paths"]["orders_dir"],
        dry_run=dry_run,
    )
    runner = Runner(selector, market_data, strategy, gateway, positions)

    now = datetime.now()
    # Run twice in dry-run mode so the sample 5-minute confirmation can be observed.
    first_count = runner.run_once(as_of=now.date(), at=now)
    second_count = 0
    if dry_run:
        second_count = runner.run_once(as_of=now.date(), at=now + timedelta(minutes=5))
    print(f"Done. Confirmed signals: {first_count + second_count}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--dry-run", action="store_true", help="print order intents without sending email")
    args = parser.parse_args()
    run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
