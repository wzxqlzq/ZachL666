import csv
import logging
from datetime import datetime
from pathlib import Path

from models import OrderIntent, Signal
from notifier import EmailNotificationService
from signal_store import SignalStore


class AlertTradeGateway:
    def __init__(
        self,
        notifier: EmailNotificationService,
        signal_store: SignalStore,
        orders_dir: str,
        dry_run: bool = False,
    ):
        self.notifier = notifier
        self.signal_store = signal_store
        self.orders_dir = Path(orders_dir)
        self.dry_run = dry_run

    def submit_signal(self, signal: Signal) -> OrderIntent:
        if not self.dry_run and self.signal_store.seen(signal):
            return self._to_order_intent(signal, "DUPLICATE")

        intent = self._to_order_intent(signal, "DRY_RUN" if self.dry_run else "NEW")

        if self.dry_run:
            print(intent)
        else:
            self._append_order(intent)
            self.notifier.send_trade_signal(signal)
            self.signal_store.mark_seen(signal)

        logging.info("Order intent recorded: %s", intent)
        return intent

    def _to_order_intent(self, signal: Signal, status: str) -> OrderIntent:
        return OrderIntent(
            symbol=signal.symbol,
            action=signal.action,
            trade_date=signal.trade_date,
            created_at=signal.confirmed_at or datetime.now(),
            reference_price=signal.price,
            reason=signal.reason,
            risk_note=signal.risk_note,
            status=status,
        )

    def _append_order(self, intent: OrderIntent) -> None:
        self.orders_dir.mkdir(parents=True, exist_ok=True)
        path = self.orders_dir / f"orders_{intent.trade_date.isoformat()}.csv"
        is_new = not path.exists()
        with path.open("a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "trade_date",
                    "created_at",
                    "symbol",
                    "action",
                    "reference_price",
                    "reason",
                    "risk_note",
                    "status",
                ],
            )
            if is_new:
                writer.writeheader()
            writer.writerow(
                {
                    "trade_date": intent.trade_date.isoformat(),
                    "created_at": intent.created_at.isoformat(sep=" ", timespec="seconds"),
                    "symbol": intent.symbol,
                    "action": intent.action,
                    "reference_price": f"{intent.reference_price:.2f}",
                    "reason": intent.reason,
                    "risk_note": intent.risk_note,
                    "status": intent.status,
                }
            )
