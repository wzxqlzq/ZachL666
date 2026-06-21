import csv
from datetime import date
from pathlib import Path

from interfaces import MarketDataProvider
from models import Bar, StockCandidate


class CsvStockSelector:
    def __init__(self, csv_path: str):
        self.csv_path = Path(csv_path)

    def select(self, as_of: date) -> list[StockCandidate]:
        if not self.csv_path.exists():
            return []

        candidates: list[StockCandidate] = []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                symbol = (row.get("symbol") or "").strip()
                if not symbol:
                    continue
                if self._is_excluded(row):
                    continue
                candidates.append(
                    StockCandidate(
                        symbol=symbol,
                        name=(row.get("name") or "").strip(),
                        reason=(row.get("reason") or "csv_candidate").strip(),
                    )
                )
        return candidates

    def _is_excluded(self, row: dict[str, str]) -> bool:
        name = row.get("name") or ""
        status = (row.get("status") or "").strip().lower()
        if "ST" in name.upper():
            return True
        return status in {"suspended", "delisted", "exclude"}


class RuleBasedStockSelector:
    def __init__(
        self,
        universe_csv_path: str,
        market_data: MarketDataProvider,
        min_avg_amount_20: float = 500_000_000,
        min_atr_ratio: float = 0.03,
        max_atr_ratio: float = 0.07,
        min_market_cap: float = 20_000_000_000,
    ):
        self.universe_csv_path = Path(universe_csv_path)
        self.market_data = market_data
        self.min_avg_amount_20 = min_avg_amount_20
        self.min_atr_ratio = min_atr_ratio
        self.max_atr_ratio = max_atr_ratio
        self.min_market_cap = min_market_cap

    def select(self, as_of: date) -> list[StockCandidate]:
        candidates: list[StockCandidate] = []
        for stock in self._load_universe():
            symbol = stock["symbol"]
            if self._is_excluded(stock):
                continue
            if self._is_beijing(stock):
                continue
            if self._market_cap(stock) <= self.min_market_cap:
                continue

            bars = self.market_data.load_daily_bars(symbol, end_date=as_of)
            if self._passes_price_and_volume_rules(bars):
                candidates.append(
                    StockCandidate(
                        symbol=symbol,
                        name=stock.get("name", ""),
                        reason="rule_based_turtle_universe",
                    )
                )
        return candidates

    def _load_universe(self) -> list[dict[str, str]]:
        if not self.universe_csv_path.exists():
            return []
        with self.universe_csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            return [
                {key: (value or "").strip() for key, value in row.items()}
                for row in csv.DictReader(f)
                if (row.get("symbol") or "").strip()
            ]

    def _is_beijing(self, row: dict[str, str]) -> bool:
        symbol = row.get("symbol", "")
        exchange = row.get("exchange", "").upper()
        return symbol.endswith(".BJ") or exchange in {"BJ", "BSE", "BEIJING"}

    def _is_excluded(self, row: dict[str, str]) -> bool:
        name = row.get("name") or ""
        status = (row.get("status") or "").strip().lower()
        if "ST" in name.upper():
            return True
        return status in {"suspended", "delisted", "exclude"}

    def _market_cap(self, row: dict[str, str]) -> float:
        raw = row.get("market_cap") or row.get("total_market_cap") or "0"
        return float(raw.replace(",", ""))

    def _passes_price_and_volume_rules(self, bars: list[Bar]) -> bool:
        if len(bars) < 121:
            return False

        latest = bars[-1]
        closes = [bar.close for bar in bars]
        ma50 = sum(closes[-50:]) / 50
        ma120 = sum(closes[-120:]) / 120
        amount20 = sum(self._amount(bar) for bar in bars[-20:]) / 20
        atr20 = self._atr(bars[-21:])
        low20 = min(bar.low for bar in bars[-20:])
        low10 = min(bar.low for bar in bars[-10:])
        prev55_high = max(bar.high for bar in bars[-56:-1])

        return all(
            [
                latest.close > ma50,
                ma50 > ma120,
                amount20 > self.min_avg_amount_20,
                self.min_atr_ratio < atr20 / latest.close < self.max_atr_ratio,
                low20 > ma120,
                low10 > ma50,
                latest.close < prev55_high,
            ]
        )

    def _amount(self, bar: Bar) -> float:
        return bar.amount if bar.amount is not None else bar.close * bar.volume

    def _atr(self, bars: list[Bar]) -> float:
        true_ranges: list[float] = []
        for previous, current in zip(bars, bars[1:]):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
        return sum(true_ranges[-20:]) / 20
