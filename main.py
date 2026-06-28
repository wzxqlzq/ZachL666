import argparse
import json
import logging
import os
import shutil
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from candidate_pool import CandidatePoolRepository
from market_data import AkshareMarketDataProvider, CsvMarketDataProvider, EastmoneyMarketDataProvider, SplitMarketDataProvider
from notifier import EmailNotificationService, SmtpEmailSender
from offline_data import OfflineDataStore, OfflineMarketDataProvider
from portfolio import PortfolioRepository
from runner import Runner
from service import ScheduledService, WeeklySelectionJob, build_weekly_update_args, parse_trading_sessions
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


def build_market_data(config: dict, provider: str | None = None):
    provider = provider or config["market_data"].get("provider", "csv")
    if provider == "csv":
        return CsvMarketDataProvider(config["market_data"]["daily_csv_path"])
    if provider == "akshare":
        return AkshareMarketDataProvider(
            lookback_days=config["market_data"].get("lookback_days", 220),
            adjust=config["market_data"].get("adjust", "qfq"),
            timeout=config["market_data"].get("timeout", 30),
            history_source=config["market_data"].get("history_source", "auto"),
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


def build_runner(config: dict, dry_run: bool) -> Runner:
    market_data = build_market_data(config)
    selector = build_selector(config, market_data)
    return build_runner_with_components(config, dry_run=dry_run, selector=selector, market_data=market_data)


def build_runner_with_components(config: dict, dry_run: bool, selector, market_data) -> Runner:
    strategy = TurtleStrategyEngine(**config["strategy"])
    portfolio_repository = PortfolioRepository(config["paths"]["portfolio"])
    positions = portfolio_repository.load_positions()
    notifier = EmailNotificationService(SmtpEmailSender(config["email"]))
    gateway = AlertTradeGateway(
        notifier=notifier,
        signal_store=SignalStore(config["paths"]["signal_db"]),
        orders_dir=config["paths"]["orders_dir"],
        dry_run=dry_run,
    )
    return Runner(selector, market_data, strategy, gateway, positions, portfolio_repository=portfolio_repository)


def build_intraday_runner(config: dict, dry_run: bool) -> Runner:
    service_config = config.get("service", {})
    intraday_config = service_config.get("intraday", {})
    candidate_pool_path = service_config.get("candidate_pool_path") or config["selector"]["csv_path"]
    offline_root = intraday_config.get("offline_root", "data/offline")
    quote_provider_name = intraday_config.get("quote_provider", "akshare")
    market_data = SplitMarketDataProvider(
        daily_bars_provider=OfflineMarketDataProvider(OfflineDataStore(offline_root)),
        quote_provider=build_market_data(config, provider=quote_provider_name),
    )
    selector = CsvStockSelector(candidate_pool_path)
    return build_runner_with_components(config, dry_run=dry_run, selector=selector, market_data=market_data)


def build_service(config: dict, dry_run: bool) -> ScheduledService:
    service_config = config.get("service", {})
    intraday_config = service_config.get("intraday", {})
    weekly_config = service_config.get("weekly_selection", {})
    candidate_pool_path = service_config.get("candidate_pool_path") or config["selector"]["csv_path"]
    now = datetime.now()
    weekly_time = datetime.strptime(weekly_config.get("time", "00:30"), "%H:%M").time()

    intraday_runner = build_intraday_runner(config, dry_run=dry_run)
    portfolio_repository = PortfolioRepository(config["paths"]["portfolio"])
    weekly_job = WeeklySelectionJob(
        portfolio_repository=portfolio_repository,
        candidate_pool_repository=CandidatePoolRepository(candidate_pool_path),
        args_factory=lambda now: build_weekly_update_args(config, now),
    )

    service = ScheduledService(
        weekly_job=weekly_job,
        intraday_job=lambda now: run_runner_once(intraday_runner, now=now),
        weekly_enabled=weekly_config.get("enabled", True),
        intraday_enabled=intraday_config.get("enabled", True),
        weekly_weekday=weekly_config.get("weekday", 6),
        weekly_time=weekly_time,
        sessions=parse_trading_sessions(intraday_config.get("sessions")),
        intraday_interval_seconds=intraday_config.get("interval_seconds", 60),
        loop_sleep_seconds=service_config.get("loop_sleep_seconds", 1.0),
    )
    if _weekly_output_exists(config, now, weekly_config.get("weekday", 6), weekly_time):
        service.mark_weekly_ran(now)
    return service


def _weekly_output_exists(config: dict, now: datetime, weekly_weekday: int, weekly_time) -> bool:
    if now.weekday() != weekly_weekday or now.time() < weekly_time:
        return False
    return Path(build_weekly_update_args(config, now).output).exists()


def prepare_runner(dry_run: bool) -> Runner:
    ensure_local_files()
    config = load_config()
    setup_logging(config["paths"]["log_file"])
    return build_runner(config, dry_run=dry_run)


def run_runner_once(runner: Runner, now: datetime | None = None) -> int:
    if runner.portfolio_repository is not None:
        runner.positions = runner.portfolio_repository.load_positions()
    now = now or datetime.now()
    signal_count = runner.run_once(as_of=now.date(), at=now)
    print(f"{now.isoformat(sep=' ', timespec='seconds')} Confirmed signals: {signal_count}")
    return signal_count


def run_once(dry_run: bool) -> None:
    runner = prepare_runner(dry_run=dry_run)
    signal_count = run_runner_once(runner)
    print(f"Done. Confirmed signals: {signal_count}")


def run_loop(dry_run: bool, interval_seconds: int) -> None:
    if interval_seconds <= 0:
        raise ValueError("--interval-seconds must be greater than 0")

    runner = prepare_runner(dry_run=dry_run)
    print(f"Starting loop. Interval: {interval_seconds}s")
    while True:
        run_runner_once(runner)
        time.sleep(interval_seconds)


def run_service(dry_run: bool) -> None:
    ensure_local_files()
    config = load_config()
    setup_logging(config["paths"]["log_file"])
    service = build_service(config, dry_run=dry_run)
    print("Starting scheduled service.")
    service.run_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--loop", action="store_true", help="scan repeatedly until stopped")
    parser.add_argument("--service", action="store_true", help="run weekly selection and intraday scans on schedule")
    parser.add_argument("--interval-seconds", type=int, default=60, help="loop scan interval in seconds")
    parser.add_argument("--dry-run", action="store_true", help="print order intents without sending email")
    args = parser.parse_args()
    if args.service:
        run_service(dry_run=args.dry_run)
    elif args.loop:
        run_loop(dry_run=args.dry_run, interval_seconds=args.interval_seconds)
    else:
        run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
