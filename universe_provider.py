import csv
import time
from pathlib import Path
from typing import Any, Protocol


class StockUniverseProvider(Protocol):
    def load_universe(self) -> list[dict[str, str]]:
        ...


class CsvStockUniverseProvider:
    def __init__(self, csv_path: str):
        self.csv_path = Path(csv_path)

    def load_universe(self) -> list[dict[str, str]]:
        if not self.csv_path.exists():
            return []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))


class AkshareStockUniverseProvider:
    def __init__(self, ak_client=None, enrich_market_cap: bool = True):
        if ak_client is None:
            try:
                import akshare as ak  # type: ignore
            except ImportError as exc:
                raise RuntimeError("Install akshare before using AkshareStockUniverseProvider") from exc
            ak_client = ak
        self.ak = ak_client
        self.enrich_market_cap = enrich_market_cap

    def load_universe(self) -> list[dict[str, str]]:
        code_name_rows = self._records(self.ak.stock_info_a_code_name())
        spot_by_code = self._safe_spot_by_code() if self.enrich_market_cap else {}
        rows: list[dict[str, str]] = []
        for row in code_name_rows:
            code = str(self._value(row, "code", "代码", default=""))
            if not code:
                continue
            spot = spot_by_code.get(code, {})
            rows.append(
                {
                    "symbol": self._symbol_with_suffix(code),
                    "name": str(self._value(row, "name", "名称", default="")),
                    "exchange": self._exchange(code),
                    "market_cap": str(self._value(spot, "总市值", "market_cap", "f20", default=0)),
                    "status": "",
                }
            )
        return rows

    def _safe_spot_by_code(self) -> dict[str, dict]:
        try:
            return self._spot_by_code()
        except Exception:
            return {}

    def _spot_by_code(self) -> dict[str, dict]:
        frame = self.ak.stock_zh_a_spot_em()
        return {
            str(self._value(row, "代码", "symbol", "f12", "f57", default="")): row
            for row in self._records(frame)
            if self._value(row, "代码", "symbol", "f12", "f57", default="")
        }

    def _records(self, frame) -> list[dict]:
        if hasattr(frame, "to_dict"):
            return frame.to_dict("records")
        return list(frame)

    def _value(self, row: dict, *names: str, default=None):
        for name in names:
            if name in row:
                return row[name]
        return default

    def _symbol_with_suffix(self, code: str) -> str:
        if code.startswith(("60", "68", "90", "51", "52", "58")):
            return f"{code}.SH"
        if code.startswith(("00", "30", "20", "15", "16", "18")):
            return f"{code}.SZ"
        if code.startswith(("43", "83", "87", "88", "92")):
            return f"{code}.BJ"
        return code

    def _exchange(self, code: str) -> str:
        symbol = self._symbol_with_suffix(code)
        return symbol.split(".")[-1] if "." in symbol else ""


class EastmoneyStockUniverseProvider:
    def __init__(self, session: Any | None = None, retries: int = 3, retry_sleep_seconds: float = 1.0):
        if session is None:
            import requests

            session = requests
        self.session = session
        self.retries = retries
        self.retry_sleep_seconds = retry_sleep_seconds
        self.headers = {"User-Agent": "Mozilla/5.0"}

    def load_universe(self) -> list[dict[str, str]]:
        url = "https://82.push2.eastmoney.com/api/qt/clist/get"
        rows: list[dict[str, str]] = []
        page = 1
        page_size = 200
        while True:
            payload = self._get_page(url, page, page_size)
            data = payload.get("data") or {}
            diff = data.get("diff") or []
            if not diff:
                break
            for item in diff:
                code = str(item.get("f12") or "")
                exchange = self._exchange_from_market(item.get("f13"))
                rows.append(
                    {
                        "symbol": f"{code}.{exchange}" if exchange else code,
                        "name": str(item.get("f14") or ""),
                        "exchange": exchange,
                        "market_cap": str(item.get("f20") or 0),
                        "status": "",
                    }
                )
            if len(rows) >= int(data.get("total") or 0):
                break
            page += 1
        return rows

    def _get_page(self, url: str, page: int, page_size: int) -> dict:
        params = {
            "pn": page,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f12",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f12,f13,f14,f20",
        }
        return self._get_json(url, params)

    def _get_json(self, url: str, params: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.get(url, params=params, timeout=20, headers=self.headers)
                response.raise_for_status()
                payload = response.json()
                if payload.get("rc") not in {0, None}:
                    raise RuntimeError(f"Eastmoney universe rc={payload.get('rc')}: {payload}")
                return payload
            except Exception as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    time.sleep(self.retry_sleep_seconds)
        raise RuntimeError(f"Eastmoney universe request failed after {self.retries} attempts: {last_error}") from last_error

    def _exchange_from_market(self, market) -> str:
        market_id = int(market or 0)
        if market_id == 1:
            return "SH"
        if market_id == 0:
            return "SZ"
        return ""


class FallbackStockUniverseProvider:
    def __init__(self, primary: StockUniverseProvider, fallback: StockUniverseProvider | None = None):
        self.primary = primary
        self.fallback = fallback

    def load_universe(self) -> list[dict[str, str]]:
        try:
            return self.primary.load_universe()
        except Exception:
            if self.fallback is None:
                raise
            return self.fallback.load_universe()
