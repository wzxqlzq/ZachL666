import argparse
from datetime import datetime

from candidate_pool import CandidatePoolRepository
from main import ensure_local_files, load_config, setup_logging
from portfolio import PortfolioRepository
from service import WeeklySelectionJob, build_weekly_update_args


def parse_run_at(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected ISO datetime, for example 2026-06-28T00:30:00") from exc


def run_weekly_selection(run_at: datetime, bar_worker_mode: str = "process", bar_workers: int = 4) -> None:
    ensure_local_files()
    config = load_config()
    setup_logging(config["paths"]["log_file"])
    service_config = config.get("service", {})
    candidate_pool_path = service_config.get("candidate_pool_path") or config["selector"]["csv_path"]

    def args_factory(now: datetime):
        args = build_weekly_update_args(config, now)
        args.bar_worker_mode = bar_worker_mode
        args.bar_workers = bar_workers
        return args

    job = WeeklySelectionJob(
        portfolio_repository=PortfolioRepository(config["paths"]["portfolio"]),
        candidate_pool_repository=CandidatePoolRepository(candidate_pool_path),
        args_factory=args_factory,
    )

    print(
        "[manual_weekly_selection] "
        f"started at {run_at.isoformat(sep=' ', timespec='seconds')} "
        f"bar_worker_mode={bar_worker_mode} bar_workers={bar_workers}"
    )
    job(run_at)
    print("[manual_weekly_selection] finished")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one full weekly selection job and refresh the candidate pool.")
    parser.add_argument(
        "--at",
        type=parse_run_at,
        default=None,
        help="run timestamp used for weekly output naming; defaults to now, accepts YYYY-MM-DD or ISO datetime",
    )
    parser.add_argument(
        "--bar-worker-mode",
        choices=["process", "serial"],
        default="process",
        help="daily-bar sync mode for this manual run; defaults to process",
    )
    parser.add_argument(
        "--bar-workers",
        type=int,
        default=4,
        help="daily-bar process workers for this manual run; defaults to 4",
    )
    args = parser.parse_args()
    run_weekly_selection(args.at or datetime.now(), bar_worker_mode=args.bar_worker_mode, bar_workers=args.bar_workers)


if __name__ == "__main__":
    main()
