import csv
import logging
import time as time_module
from argparse import Namespace
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable

from candidate_pool import CandidatePoolRepository
from models import StockCandidate
from portfolio import PortfolioRepository
from run_weekly_update import (
    DEFAULT_VALIDATION_LOOKBACK_DAYS,
    DEFAULT_VALIDATION_WINDOW,
    run_weekly_update,
)


@dataclass(frozen=True)
class TradingSession:
    start: time
    end: time

    @classmethod
    def parse(cls, raw: str) -> "TradingSession":
        start, end = raw.split("-", 1)
        return cls(start=_parse_time(start), end=_parse_time(end))

    def contains(self, current: time) -> bool:
        return self.start <= current <= self.end


class ScheduledService:
    def __init__(
        self,
        weekly_job: Callable[[datetime], None],
        intraday_job: Callable[[datetime], None],
        *,
        weekly_enabled: bool = True,
        intraday_enabled: bool = True,
        weekly_weekday: int = 6,
        weekly_time: time = time(0, 30),
        sessions: list[TradingSession] | None = None,
        intraday_interval_seconds: int = 60,
        loop_sleep_seconds: float = 1.0,
        now_func: Callable[[], datetime] | None = None,
        sleep_func: Callable[[float], None] | None = None,
        is_trading_day: Callable[[date], bool] | None = None,
    ):
        self.weekly_job = weekly_job
        self.intraday_job = intraday_job
        self.weekly_enabled = weekly_enabled
        self.intraday_enabled = intraday_enabled
        self.weekly_weekday = weekly_weekday
        self.weekly_time = weekly_time
        self.sessions = sessions or default_trading_sessions()
        self.intraday_interval_seconds = intraday_interval_seconds
        self.loop_sleep_seconds = loop_sleep_seconds
        self.now_func = now_func or datetime.now
        self.sleep_func = sleep_func or time_module.sleep
        self.is_trading_day = is_trading_day or is_simple_weekday
        self._last_weekly_key: tuple[int, int] | None = None
        self._last_intraday_scan: datetime | None = None

    def run_forever(self) -> None:
        while True:
            self.tick(self.now_func())
            self.sleep_func(self.loop_sleep_seconds)

    def mark_weekly_ran(self, now: datetime) -> None:
        self._last_weekly_key = self._week_key(now)

    def tick(self, now: datetime | None = None) -> None:
        current = now or self.now_func()
        if self.weekly_enabled and self._should_run_weekly(current):
            self._last_weekly_key = self._week_key(current)
            self._run_job("weekly_selection", self.weekly_job, current)
        if self.intraday_enabled and self._should_run_intraday(current):
            self._last_intraday_scan = self._scan_key(current)
            self._run_job("intraday_scan", self.intraday_job, current)

    def _should_run_weekly(self, now: datetime) -> bool:
        if now.weekday() != self.weekly_weekday:
            return False
        if now.time() < self.weekly_time:
            return False
        return self._last_weekly_key != self._week_key(now)

    def _should_run_intraday(self, now: datetime) -> bool:
        if self.intraday_interval_seconds <= 0:
            return False
        if not self.is_trading_day(now.date()):
            return False
        if not any(session.contains(now.time()) for session in self.sessions):
            return False
        scan_key = self._scan_key(now)
        if self._last_intraday_scan is None:
            return True
        elapsed = (scan_key - self._last_intraday_scan).total_seconds()
        return elapsed >= self.intraday_interval_seconds

    def _run_job(self, name: str, job: Callable[[datetime], None], now: datetime) -> None:
        logging.info("[service] %s started at %s", name, now.isoformat(sep=" ", timespec="seconds"))
        print(f"[service] {name} started at {now.isoformat(sep=' ', timespec='seconds')}")
        try:
            job(now)
        except Exception as exc:
            logging.exception("[service] %s failed", name)
            print(f"[service] {name} failed: {type(exc).__name__}: {exc}")
            return
        logging.info("[service] %s finished", name)
        print(f"[service] {name} finished")

    def _week_key(self, now: datetime) -> tuple[int, int]:
        calendar = now.isocalendar()
        return calendar.year, calendar.week

    def _scan_key(self, now: datetime) -> datetime:
        return now.replace(second=0, microsecond=0)


class WeeklySelectionJob:
    def __init__(
        self,
        portfolio_repository: PortfolioRepository,
        candidate_pool_repository: CandidatePoolRepository,
        args_factory: Callable[[datetime], Namespace],
        update_func: Callable[[Namespace], tuple[int, int, int, int, Path]] | None = None,
    ):
        self.portfolio_repository = portfolio_repository
        self.candidate_pool_repository = candidate_pool_repository
        self.args_factory = args_factory
        self.update_func = update_func or run_weekly_update

    def __call__(self, now: datetime) -> None:
        args = self.args_factory(now)
        bar_count, bar_failures, market_cap_count, market_cap_failures, output_path = self.update_func(args)
        selected = load_selection_result(output_path)
        positions = self.portfolio_repository.load_positions()
        pool = self.candidate_pool_repository.replace_with_weekly_selection(selected, positions)
        logging.info(
            "[service] weekly candidate pool written path=%s selected=%s pool=%s bars=%s bar_failures=%s "
            "market_caps=%s market_cap_failures=%s",
            self.candidate_pool_repository.csv_path,
            len(selected),
            len(pool),
            bar_count,
            bar_failures,
            market_cap_count,
            market_cap_failures,
        )
        print(
            "[service] candidate pool updated: "
            f"path={self.candidate_pool_repository.csv_path} selected={len(selected)} pool={len(pool)}"
        )


def build_weekly_update_args(config: dict, now: datetime) -> Namespace:
    service_config = config.get("service", {})
    weekly_config = service_config.get("weekly_selection", {})
    output = weekly_config.get("output")
    if not output:
        output_dir = weekly_config.get("output_dir", "selection_results")
        output = str(Path(output_dir) / f"weekly_{now.date().isoformat()}.csv")

    return Namespace(
        root=weekly_config.get("root", "data/offline"),
        output=output,
        lookback_days=weekly_config.get("lookback_days", 14),
        workers=weekly_config.get("workers", 4),
        limit=weekly_config.get("limit", 0),
        failures_csv=weekly_config.get("failures_csv", "data/offline/sync_failures.csv"),
        request_delay=weekly_config.get("request_delay", 0.5),
        max_retries=weekly_config.get("max_retries", 2),
        provider=weekly_config.get("provider", "akshare"),
        fallback=weekly_config.get("fallback", "eastmoney"),
        akshare_timeout=weekly_config.get("akshare_timeout", 30),
        akshare_history_source=weekly_config.get("akshare_history_source", "sina"),
        market_cap_provider=weekly_config.get("market_cap_provider", "eastmoney"),
        market_cap_fallback=weekly_config.get("market_cap_fallback", "tencent"),
        market_cap_page_size=weekly_config.get("market_cap_page_size", 100),
        force_market_cap_refresh=weekly_config.get("force_market_cap_refresh", False),
        target_date=weekly_config.get("target_date", "auto"),
        target_probe_symbol=weekly_config.get("target_probe_symbol", "000001.SZ"),
        update_scope=weekly_config.get("update_scope", "selection"),
        bar_worker_mode=weekly_config.get("bar_worker_mode", "process"),
        bar_workers=weekly_config.get("bar_workers", 4),
        bar_batch_size=weekly_config.get("bar_batch_size", 20),
        bar_timeout_seconds=weekly_config.get("bar_timeout_seconds", 15),
        final_retry_provider=weekly_config.get("final_retry_provider", "eastmoney"),
        skip_up_to_date_bars=weekly_config.get("skip_up_to_date_bars", True),
        validate_offline_bars=weekly_config.get("validate_offline_bars", True),
        validation_window=weekly_config.get("validation_window", DEFAULT_VALIDATION_WINDOW),
        validation_lookback_days=weekly_config.get("validation_lookback_days", DEFAULT_VALIDATION_LOOKBACK_DAYS),
        skip_existing_market_cap=weekly_config.get("skip_existing_market_cap", False),
        skip_market_cap_refresh=weekly_config.get("skip_market_cap_refresh", False),
        notify_selection=weekly_config.get("notify_selection", False),
    )


def load_selection_result(path: Path) -> list[StockCandidate]:
    candidates: list[StockCandidate] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            symbol = (row.get("symbol") or "").strip()
            if not symbol:
                continue
            candidates.append(
                StockCandidate(
                    symbol=symbol,
                    name=(row.get("name") or "").strip(),
                    reason=(row.get("reason") or "").strip(),
                )
            )
    return candidates


def default_trading_sessions() -> list[TradingSession]:
    return [TradingSession.parse("09:30-11:30"), TradingSession.parse("13:00-15:00")]


def parse_trading_sessions(raw_sessions: list[str] | None) -> list[TradingSession]:
    if not raw_sessions:
        return default_trading_sessions()
    return [TradingSession.parse(raw) for raw in raw_sessions]


def is_simple_weekday(current: date) -> bool:
    return current.weekday() < 5


def _parse_time(raw: str) -> time:
    return datetime.strptime(raw.strip(), "%H:%M").time()
