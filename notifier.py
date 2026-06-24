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
        msg.set_content(body, charset="utf-8")

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
        subject = f"[交易提醒] {signal.action} {signal.symbol} {signal.trade_date}"
        body = "\n".join(
            self._trade_signal_lines(signal)
            + [
                "",
                "这是一条交易提醒。下单前请人工复核价格、仓位和账户状态。",
            ]
        )
        self.sender.send(subject, body)

    def _trade_signal_lines(self, signal: Signal) -> list[str]:
        lines = [
            f"标的: {signal.symbol}",
            f"动作: {signal.action}",
            f"交易日期: {signal.trade_date}",
            f"参考价格: {signal.price:.2f}",
            f"触发原因: {signal.reason}",
        ]
        if signal.suggested_shares is not None:
            lines.extend(
                [
                    f"建议股数: {signal.suggested_shares}",
                    f"参考金额: {signal.suggested_notional:.2f}",
                    f"ATR: {signal.atr:.4f}",
                    f"止损价: {signal.stop_loss:.4f}",
                    f"目标风险金额: {signal.risk_amount:.2f}",
                ]
            )
        return lines

    def send_signal(self, signal: Signal) -> None:
        self.send_trade_signal(signal)

    def send_selection_report(self, report: SelectionReport) -> None:
        subject = (
            f"[选股报告] {report.as_of.isoformat()} "
            f"初筛={len(report.before_trend_filter)} 入选={len(report.selected)}"
        )
        body = "\n".join(
            [
                f"目标交易日: {report.as_of.isoformat()}",
                f"输出文件: {report.output_path}",
                "",
                f"趋势过滤前: {len(report.before_trend_filter)}",
                self._format_candidates(report.before_trend_filter),
                "",
                f"趋势过滤后入选: {len(report.selected)}",
                self._format_candidates(report.selected),
                "",
                f"因已有海龟趋势排除: {len(report.excluded_by_active_trend)}",
                self._format_candidates(report.excluded_by_active_trend),
            ]
        )
        self.sender.send(subject, body)

    def _format_candidates(self, candidates: list[StockCandidate]) -> str:
        if not candidates:
            return "(无)"
        return "\n".join(f"- {self._format_candidate(candidate)}" for candidate in candidates)

    def _format_candidate(self, candidate: StockCandidate) -> str:
        if candidate.name:
            return f"{candidate.symbol} {candidate.name}"
        return candidate.symbol


EmailNotifier = EmailNotificationService
