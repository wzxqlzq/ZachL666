import csv
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from models import Bar, Quote, StockCandidate


class CsvMarketDataProvider:
    def __init__(self, daily_csv_path: str):
        self.daily_csv_path = Path(daily_csv_path)

    def load_daily_bars(self, symbol: str, end_date: date | None = None) -> list[Bar]:
        bars: list[Bar] = []
        with self.daily_csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row["symbol"] != symbol:
                    continue
                trade_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
                if end_date and trade_date > end_date:
                    continue
                bars.append(
                    Bar(
                        symbol=row["symbol"],
                        trade_date=trade_date,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(row["volume"]),
                        amount=float(row["amount"]) if row.get("amount") else None,
                    )
                )
        return sorted(bars, key=lambda item: item.trade_date)

    def get_quote(self, symbol: str, at: datetime | None = None) -> Quote:
        bars = self.load_daily_bars(symbol)
        if not bars:
            raise ValueError(f"No CSV bars found for {symbol}")
        latest = bars[-1]
        timestamp = at or datetime.combine(latest.trade_date, datetime.min.time())
        return Quote(
            symbol=symbol,
            timestamp=timestamp,
            price=latest.close,
            open=latest.open,
            high=latest.high,
            low=latest.low,
            volume=latest.volume,
        )


class AkshareMarketDataProvider:
    def __init__(
        self,
        lookback_days: int = 220,
        adjust: str = "qfq",
        timeout: float | None = 30,
        history_source: str = "auto",
        ak_client=None,
    ) -> None:
        if ak_client is None:
            try:
                import akshare as ak  # type: ignore
            except ImportError as exc:
                raise RuntimeError("Install akshare before using AkshareMarketDataProvider: pip install akshare") from exc
            ak_client = ak
        self.ak = ak_client
        self.lookback_days = lookback_days
        self.adjust = adjust
        self.timeout = timeout
        self.history_source = history_source
        self._spot_cache: dict[str, dict] | None = None

    def load_daily_bars(self, symbol: str, end_date: date | None = None) -> list[Bar]:
        if self.history_source == "eastmoney":
            return self._load_daily_bars_from_hist(symbol, end_date=end_date)
        if self.history_source == "sina":
            return self._load_daily_bars_from_daily(symbol, end_date=end_date)
        if self.history_source != "auto":
            raise ValueError(f"Unsupported AKShare history source: {self.history_source}")
        try:
            return self._load_daily_bars_from_hist(symbol, end_date=end_date)
        except Exception:
            return self._load_daily_bars_from_daily(symbol, end_date=end_date)

    def _load_daily_bars_from_hist(self, symbol: str, end_date: date | None = None) -> list[Bar]:
        end = end_date or date.today()
        start = end - timedelta(days=self.lookback_days)
        df = self.ak.stock_zh_a_hist(
            symbol=self._plain_symbol(symbol),
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=self.adjust,
            timeout=self.timeout,
        )
        bars: list[Bar] = []
        for row in self._records(df):
            trade_date = self._parse_date(self._value(row, "日期", "date"))
            bars.append(
                Bar(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=self._float(self._value(row, "开盘", "open")),
                    high=self._float(self._value(row, "最高", "high")),
                    low=self._float(self._value(row, "最低", "low")),
                    close=self._float(self._value(row, "收盘", "close")),
                    volume=int(self._float(self._value(row, "成交量", "volume", default=0)) * 100),
                    amount=self._float_or_none(self._value(row, "成交额", "amount", default=None)),
                )
            )
        return sorted(bars, key=lambda item: item.trade_date)

    def _load_daily_bars_from_daily(self, symbol: str, end_date: date | None = None) -> list[Bar]:
        end = end_date or date.today()
        start = end - timedelta(days=self.lookback_days)
        df = self.ak.stock_zh_a_daily(
            symbol=self._sina_symbol(symbol),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=self.adjust,
        )
        bars: list[Bar] = []
        for row in self._records(df):
            trade_date = self._parse_date(self._value(row, "date", "日期"))
            bars.append(
                Bar(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=self._float(self._value(row, "open", "开盘")),
                    high=self._float(self._value(row, "high", "最高")),
                    low=self._float(self._value(row, "low", "最低")),
                    close=self._float(self._value(row, "close", "收盘")),
                    volume=int(self._float(self._value(row, "volume", "成交量", default=0))),
                    amount=self._float_or_none(self._value(row, "amount", "成交额", default=None)),
                )
            )
        return sorted(bars, key=lambda item: item.trade_date)

    def get_quote(self, symbol: str, at: datetime | None = None) -> Quote:
        failures: list[str] = []
        sources = [
            ("stock_zh_a_spot_em", self._quote_from_spot_em),
            ("stock_zh_a_minute", self._quote_from_sina_minute),
            ("stock_zh_a_spot", self._quote_from_sina_spot),
            ("stock_zh_a_tick_tx_js", self._quote_from_tx_tick),
            ("stock_individual_spot_xq", self._quote_from_xueqiu_spot),
        ]
        for source_name, loader in sources:
            try:
                quote = loader(symbol, at=at)
                logging.info("[quote_fallback] source=%s symbol=%s succeeded", source_name, symbol)
                return quote
            except Exception as exc:
                message = f"[quote_fallback] source={source_name} symbol={symbol} failed: {type(exc).__name__}: {exc}"
                failures.append(message)
                logging.warning(message)
                print(message)
        raise RuntimeError(f"All AKShare quote sources failed for {symbol}: " + " | ".join(failures))

    def _quote_from_spot_em(self, symbol: str, at: datetime | None = None) -> Quote:
        df = self.ak.stock_zh_a_spot_em()
        spot = self._row_by_symbol(df, symbol, "代码", "symbol", "f57")
        if not spot:
            raise ValueError(f"No stock_zh_a_spot_em quote found for {symbol}")
        return Quote(
            symbol=symbol,
            timestamp=at or datetime.now(),
            price=self._spot_price(spot, "最新价", "price", "f43"),
            open=self._spot_price_or_none(spot, "今开", "open", "f46"),
            high=self._spot_price_or_none(spot, "最高", "high", "f44"),
            low=self._spot_price_or_none(spot, "最低", "low", "f45"),
            volume=int(self._float(self._value(spot, "成交量", "volume", "f47", default=0))),
        )

    def _quote_from_sina_minute(self, symbol: str, at: datetime | None = None) -> Quote:
        df = self.ak.stock_zh_a_minute(symbol=self._sina_symbol(symbol), period="1", adjust="")
        records = self._records(df)
        if not records:
            raise ValueError(f"No stock_zh_a_minute rows found for {symbol}")
        latest = records[-1]
        timestamp = at or self._parse_datetime(self._value(latest, "day", "datetime", "time", default=None))
        return Quote(
            symbol=symbol,
            timestamp=timestamp,
            price=self._float(self._value(latest, "close", "收盘")),
            open=self._float_or_none(self._value(latest, "open", "开盘", default=None)),
            high=self._float_or_none(self._value(latest, "high", "最高", default=None)),
            low=self._float_or_none(self._value(latest, "low", "最低", default=None)),
            volume=int(self._float(self._value(latest, "volume", "成交量", default=0))),
        )

    def _quote_from_sina_spot(self, symbol: str, at: datetime | None = None) -> Quote:
        df = self.ak.stock_zh_a_spot()
        spot = self._row_by_symbol(df, symbol, "代码", "symbol")
        if not spot:
            raise ValueError(f"No stock_zh_a_spot quote found for {symbol}")
        return Quote(
            symbol=symbol,
            timestamp=at or datetime.now(),
            price=self._spot_price(spot, "最新价", "price", "trade"),
            open=self._spot_price_or_none(spot, "今开", "open"),
            high=self._spot_price_or_none(spot, "最高", "high"),
            low=self._spot_price_or_none(spot, "最低", "low"),
            volume=int(self._float(self._value(spot, "成交量", "volume", default=0))),
        )

    def _quote_from_tx_tick(self, symbol: str, at: datetime | None = None) -> Quote:
        df = self.ak.stock_zh_a_tick_tx_js(symbol=self._sina_symbol(symbol))
        records = self._records(df)
        if not records:
            raise ValueError(f"No stock_zh_a_tick_tx_js rows found for {symbol}")
        latest = records[-1]
        return Quote(
            symbol=symbol,
            timestamp=at or datetime.now(),
            price=self._float(self._value(latest, "成交价格", "price")),
            volume=int(self._float(self._value(latest, "成交量", "volume", default=0))),
        )

    def _quote_from_xueqiu_spot(self, symbol: str, at: datetime | None = None) -> Quote:
        df = self.ak.stock_individual_spot_xq(symbol=self._xueqiu_symbol(symbol), timeout=self.timeout)
        spot = self._item_value_rows(df)
        if not spot:
            raise ValueError(f"No stock_individual_spot_xq quote found for {symbol}")
        return Quote(
            symbol=symbol,
            timestamp=at or datetime.now(),
            price=self._float(self._value(spot, "现价", "鐜颁环", "current", "price")),
            open=self._float_or_none(self._value(spot, "今开", "浠婂紑", "open", default=None)),
            high=self._float_or_none(self._value(spot, "最高", "鏈€楂?", "high", default=None)),
            low=self._float_or_none(self._value(spot, "最低", "鏈€浣?", "low", default=None)),
            volume=int(self._float(self._value(spot, "成交量", "鎴愪氦閲?", "volume", default=0))),
        )

    def load_spot_candidates(self) -> list[StockCandidate]:
        candidates: list[StockCandidate] = []
        for row in self._spot_by_symbol().values():
            symbol = self._symbol_with_suffix(str(self._value(row, "代码", "symbol", "f57")))
            candidates.append(
                StockCandidate(
                    symbol=symbol,
                    name=str(self._value(row, "名称", "name", "f58", default="")),
                    reason="akshare_spot",
                )
            )
        return candidates

    def export_stock_universe_csv(self, path: str) -> int:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for row in self._spot_by_symbol().values():
            code = str(self._value(row, "代码", "symbol", "f57"))
            rows.append(
                {
                    "symbol": self._symbol_with_suffix(code),
                    "name": str(self._value(row, "名称", "name", "f58", default="")),
                    "exchange": self._exchange(code),
                    "market_cap": self._float(self._value(row, "总市值", "market_cap", "f20", default=0)),
                    "status": "",
                }
            )
        with target.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["symbol", "name", "exchange", "market_cap", "status"])
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    def _spot_by_symbol(self) -> dict[str, dict]:
        if self._spot_cache is None:
            df = self.ak.stock_zh_a_spot_em()
            self._spot_cache = {
                self._plain_code(str(self._value(row, "代码", "symbol", "f57"))): row
                for row in self._records(df)
                if self._value(row, "代码", "symbol", "f57", default=None)
            }
        return self._spot_cache

    def _plain_symbol(self, symbol: str) -> str:
        return symbol.split(".")[0]

    def _sina_symbol(self, symbol: str) -> str:
        code = self._plain_symbol(symbol)
        suffix = symbol.split(".")[-1].lower() if "." in symbol else ""
        if suffix in {"sh", "sz", "bj"}:
            return f"{suffix}{code}"
        if code.startswith(("60", "68", "90", "51", "52", "58")):
            return f"sh{code}"
        if code.startswith(("43", "83", "87", "88", "92")):
            return f"bj{code}"
        return f"sz{code}"

    def _xueqiu_symbol(self, symbol: str) -> str:
        code = self._plain_symbol(symbol)
        suffix = symbol.split(".")[-1].upper() if "." in symbol else ""
        if suffix in {"SH", "SZ", "BJ"}:
            return f"{suffix}{code}"
        if code.startswith(("60", "68", "90", "51", "52", "58")):
            return f"SH{code}"
        if code.startswith(("43", "83", "87", "88", "92")):
            return f"BJ{code}"
        return f"SZ{code}"

    def _plain_code(self, raw: str) -> str:
        code = str(raw).strip()
        if len(code) >= 8 and code[:2].lower() in {"sh", "sz", "bj"}:
            return code[2:]
        if len(code) >= 8 and code[:2].upper() in {"SH", "SZ", "BJ"}:
            return code[2:]
        return code

    def _symbol_with_suffix(self, code: str) -> str:
        code = self._plain_code(code)
        if code.startswith(("60", "68", "90", "51", "52", "58")):
            return f"{code}.SH"
        if code.startswith(("00", "30", "20", "15", "16", "18")):
            return f"{code}.SZ"
        if code.startswith(("43", "83", "87", "88", "92")):
            return f"{code}.BJ"
        return code

    def _exchange(self, code: str) -> str:
        suffix = self._symbol_with_suffix(code).split(".")[-1]
        return suffix if suffix != code else ""

    def _records(self, frame) -> list[dict]:
        if hasattr(frame, "to_dict"):
            return frame.to_dict("records")
        return list(frame)

    def _row_by_symbol(self, frame, symbol: str, *code_names: str) -> dict | None:
        target = self._plain_symbol(symbol)
        for row in self._records(frame):
            raw_code = self._value(row, *code_names, default=None)
            if raw_code is not None and self._plain_code(str(raw_code)) == target:
                return row
        return None

    def _item_value_rows(self, frame) -> dict:
        result = {}
        for row in self._records(frame):
            item = self._value(row, "item", "项目", default=None)
            value = self._value(row, "value", "值", default=None)
            if item is not None:
                result[str(item)] = value
        return result

    def _value(self, row: dict, *names: str, default=None):
        for name in names:
            if name in row:
                return row[name]
        return default

    def _matched_value(self, row: dict, *names: str, default=None):
        for name in names:
            if name in row:
                return name, row[name]
        return None, default

    def _parse_date(self, raw) -> date:
        if isinstance(raw, date) and not isinstance(raw, datetime):
            return raw
        if isinstance(raw, datetime):
            return raw.date()
        return datetime.strptime(str(raw), "%Y-%m-%d").date()

    def _parse_datetime(self, raw) -> datetime:
        if isinstance(raw, datetime):
            return raw
        if raw is None or raw == "":
            return datetime.now()
        text = str(raw)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                if fmt.startswith("%H"):
                    now = datetime.now()
                    return parsed.replace(year=now.year, month=now.month, day=now.day)
                return parsed
            except ValueError:
                continue
        return datetime.now()

    def _float(self, raw) -> float:
        if raw is None or raw == "":
            return 0.0
        return float(str(raw).replace(",", ""))

    def _float_or_none(self, raw) -> float | None:
        if raw is None or raw == "":
            return None
        return self._float(raw)

    def _spot_price(self, row: dict, *names: str) -> float:
        matched_name, raw = self._matched_value(row, *names)
        if matched_name and matched_name.startswith("f"):
            return self._float(raw) / 100
        return self._float(raw)

    def _spot_price_or_none(self, row: dict, *names: str) -> float | None:
        matched_name, raw = self._matched_value(row, *names, default=None)
        if raw is None or raw == "":
            return None
        if matched_name and matched_name.startswith("f"):
            return self._float(raw) / 100
        return self._float(raw)


class EastmoneyMarketDataProvider:
    def __init__(
        self,
        lookback_days: int = 220,
        adjust: str = "qfq",
        session: Any | None = None,
        retries: int = 3,
        retry_sleep_seconds: float = 1.0,
    ):
        if session is None:
            import requests

            session = requests
        self.session = session
        self.lookback_days = lookback_days
        self.adjust = adjust
        self.retries = retries
        self.retry_sleep_seconds = retry_sleep_seconds
        self.headers = {"User-Agent": "Mozilla/5.0"}

    def load_daily_bars(self, symbol: str, end_date: date | None = None) -> list[Bar]:
        end = end_date or date.today()
        start = end - timedelta(days=self.lookback_days)
        payload = self._get_json(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {
                "secid": self._secid(symbol),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "klt": "101",
                "fqt": self._fqt(),
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
            },
        )
        klines = (payload.get("data") or {}).get("klines") or []
        bars: list[Bar] = []
        for item in klines:
            fields = str(item).split(",")
            if len(fields) < 7:
                continue
            bars.append(
                Bar(
                    symbol=symbol,
                    trade_date=datetime.strptime(fields[0], "%Y-%m-%d").date(),
                    open=float(fields[1]),
                    close=float(fields[2]),
                    high=float(fields[3]),
                    low=float(fields[4]),
                    volume=int(float(fields[5]) * 100),
                    amount=float(fields[6]),
                )
            )
        return bars

    def get_quote(self, symbol: str, at: datetime | None = None) -> Quote:
        payload = self._get_json(
            "https://82.push2.eastmoney.com/api/qt/ulist.np/get",
            {
                "secids": self._secid(symbol),
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": "2",
                "invt": "2",
                "fields": "f12,f14,f2,f5,f15,f16,f17",
            },
        )
        data = self._quote_data(payload)
        if not data:
            raise ValueError(f"No Eastmoney quote found for {symbol}")
        return Quote(
            symbol=symbol,
            timestamp=at or datetime.now(),
            price=self._scaled_price(data.get("f2", data.get("f43"))),
            open=self._scaled_price(data.get("f17", data.get("f46"))),
            high=self._scaled_price(data.get("f15", data.get("f44"))),
            low=self._scaled_price(data.get("f16", data.get("f45"))),
            volume=int(float(data.get("f5", data.get("f47") or 0) or 0)),
        )

    def _get_json(self, url: str, params: dict[str, str]):
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.get(url, params=params, timeout=20, headers=self.headers)
                response.raise_for_status()
                payload = response.json()
                if payload.get("rc") not in {0, None}:
                    raise ValueError(f"Eastmoney returned rc={payload.get('rc')}: {payload}")
                return payload
            except Exception as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    time.sleep(self.retry_sleep_seconds)
        raise RuntimeError(f"Eastmoney request failed after {self.retries} attempts: {last_error}") from last_error

    def _quote_data(self, payload: dict) -> dict:
        data = payload.get("data") or {}
        diff = data.get("diff")
        if isinstance(diff, list):
            return diff[0] if diff else {}
        return data

    def _secid(self, symbol: str) -> str:
        code = symbol.split(".")[0]
        suffix = symbol.split(".")[-1].upper() if "." in symbol else ""
        if suffix == "SH" or code.startswith(("60", "68", "90", "51", "52", "58")):
            return f"1.{code}"
        if suffix == "BJ" or code.startswith(("43", "83", "87", "88", "92")):
            return f"0.{code}"
        return f"0.{code}"

    def _fqt(self) -> str:
        return {"none": "0", "qfq": "1", "hfq": "2"}.get(self.adjust, "1")

    def _scaled_price(self, raw) -> float | None:
        if raw is None or raw == "-":
            return None
        return float(raw) / 100


class FallbackMarketDataProvider:
    def __init__(self, primary, fallback=None):
        self.primary = primary
        self.fallback = fallback

    def load_daily_bars(self, symbol: str, end_date: date | None = None) -> list[Bar]:
        try:
            return self.primary.load_daily_bars(symbol, end_date=end_date)
        except Exception:
            if self.fallback is None:
                raise
            return self.fallback.load_daily_bars(symbol, end_date=end_date)

    def get_quote(self, symbol: str, at: datetime | None = None) -> Quote:
        try:
            return self.primary.get_quote(symbol, at=at)
        except Exception:
            if self.fallback is None:
                raise
            return self.fallback.get_quote(symbol, at=at)


class SplitMarketDataProvider:
    def __init__(self, daily_bars_provider, quote_provider):
        self.daily_bars_provider = daily_bars_provider
        self.quote_provider = quote_provider

    def load_daily_bars(self, symbol: str, end_date: date | None = None) -> list[Bar]:
        return self.daily_bars_provider.load_daily_bars(symbol, end_date=end_date)

    def get_quote(self, symbol: str, at: datetime | None = None) -> Quote:
        return self.quote_provider.get_quote(symbol, at=at)
