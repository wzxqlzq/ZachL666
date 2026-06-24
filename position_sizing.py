from dataclasses import dataclass
from math import floor


@dataclass(frozen=True)
class PositionSize:
    atr: float
    risk_amount: float
    shares: int
    notional: float
    stop_loss: float
    unit: int = 1


class AtrPositionSizer:
    def __init__(
        self,
        account_equity: float,
        risk_per_trade: float,
        lot_size: int = 100,
        stop_atr_multiple: float = 2.0,
    ):
        self.account_equity = account_equity
        self.risk_per_trade = risk_per_trade
        self.lot_size = lot_size
        self.stop_atr_multiple = stop_atr_multiple

    def size(self, price: float, atr: float, unit: int = 1) -> PositionSize:
        if price <= 0:
            raise ValueError("price must be greater than 0")
        if atr <= 0:
            raise ValueError("atr must be greater than 0")
        if self.account_equity <= 0:
            raise ValueError("account_equity must be greater than 0")
        if self.risk_per_trade <= 0:
            raise ValueError("risk_per_trade must be greater than 0")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be greater than 0")

        risk_amount = self.account_equity * self.risk_per_trade
        risk_per_share = atr * self.stop_atr_multiple
        raw_shares = risk_amount / risk_per_share
        shares = floor(raw_shares / self.lot_size) * self.lot_size
        return PositionSize(
            atr=atr,
            risk_amount=risk_amount,
            shares=shares,
            notional=shares * price,
            stop_loss=max(0.0, price - self.stop_atr_multiple * atr),
            unit=unit,
        )
