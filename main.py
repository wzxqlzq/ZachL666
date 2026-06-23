import argparse
import json
import logging
import os
import shutil
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from market_data import AkshareMarketDataProvider, CsvMarketDataProvider, EastmoneyMarketDataProvider
from notifier import EmailNotificationService, SmtpEmailSender
from portfolio import PortfolioRepository
from runner import Runner
from stock_selector import CsvStockSelector, RuleBasedStockSelector
from signal_store import SignalStore
from strategy import TurtleStrategyEngine
from trade_gateway import AlertTradeGateway


EMAIL_ENV_OVERRIDES = {
    "SMTP_HOST": ("smtp_host", str),
    "SMTP_PORT": ("smtp_port", int),
    "SMTP_USERNAME": ("username", str),
    "SMTP_PASSWORD": ("password", str),
    "SMTP_SENDER": ("sender", str),
    "SMTP_RECIPIENTS": ("recipients", lambda value: [item.strip() for item in value.split(",") if item.strip()]),
}


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


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
    load_dotenv()
    defaults = load_json("config.example.json")
    local = load_json("config.json") if Path("config.json").exists() else {}
    config = deep_merge(defaults, local)
    apply_env_overrides(config)
    return config


def apply_env_overrides(config: dict) -> None:
    email_config = config.setdefault("email", {})
    for env_name, (config_key, parser) in EMAIL_ENV_OVERRIDES.items():
        raw_value = os.environ.get(env_name)
        if raw_value is None:
            continue
        email_config[config_key] = parser(raw_value)


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
    notifier = EmailNotificationService(SmtpEmailSender(config["email"]))
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
