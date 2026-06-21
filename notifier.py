import smtplib
from email.message import EmailMessage

from models import Signal


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


class EmailNotifier:
    def __init__(self, sender: EmailSender):
        self.sender = sender

    def send_signal(self, signal: Signal) -> None:
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
