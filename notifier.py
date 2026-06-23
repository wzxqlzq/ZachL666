import smtplib
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from models import Signal, StockCandidate


class EmailSender:
    def send(self, subject: str, body: str) -> None:
        raise NotImplementedError


class SmtpEmailSender(EmailSender):
    def __init__(self, config: dict):
        self.config = config

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.config["sender"]
        msg["To"] = ", ".join(self.config["recipients"])
        msg.set_content(body)

        with smtplib.SMTP_SSL(self.config["smtp_host"], int(self.config["smtp_port"])) as smtp:
            smtp.login(self.config["username"], self.config["password"])
            smtp.send_message(msg)


@dataclass(frozen=True)
class SelectionReport:
    as_of: date
    output_path: Path
    before_trend_filter: list[StockCandidate]
    selected: list[StockCandidate]
    excluded_by_active_trend: list[StockCandidate]


class EmailNotificationService:
    def __init__(self, sender: EmailSender):
        self.sender = sender

    def send_trade_signal(self, signal: Signal) -> None:
        subject = f"[Trading Alert] {signal.action} {signal.symbol} {signal.trade_date}"
        body = "\n".join(
            [
                f"Symbol: {signal.symbol}",
                f"Action: {signal.action}",
                f"Date: {signal.trade_date}",
                f"Reference price: {signal.price:.2f}",
                f"Reason: {signal.reason}",
                f"Risk note: {signal.risk_note}",
                "",
                "This is an alert only. Review manually before placing any order.",
            ]
        )
        self.sender.send(subject, body)

    def send_signal(self, signal: Signal) -> None:
        self.send_trade_signal(signal)

    def send_selection_report(self, report: SelectionReport) -> None:
        subject = (
            f"[Stock Selection] {report.as_of.isoformat()} "
            f"before={len(report.before_trend_filter)} selected={len(report.selected)}"
        )
        body = "\n".join(
            [
                f"Target trade date: {report.as_of.isoformat()}",
                f"Output path: {report.output_path}",
                "",
                f"Before active trend filter: {len(report.before_trend_filter)}",
                self._format_candidates(report.before_trend_filter),
                "",
                f"Selected after active trend filter: {len(report.selected)}",
                self._format_candidates(report.selected),
                "",
                f"Excluded by active trend: {len(report.excluded_by_active_trend)}",
                self._format_candidates(report.excluded_by_active_trend),
            ]
        )
        self.sender.send(subject, body)

    def _format_candidates(self, candidates: list[StockCandidate]) -> str:
        if not candidates:
            return "(none)"
        return "\n".join(f"- {self._format_candidate(candidate)}" for candidate in candidates)

    def _format_candidate(self, candidate: StockCandidate) -> str:
        if candidate.name:
            return f"{candidate.symbol} {candidate.name}"
        return candidate.symbol


EmailNotifier = EmailNotificationService
