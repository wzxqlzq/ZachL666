import csv
import time
from decimal import Decimal
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
    def __init__(
        self,
        session: Any | None = None,
        retries: int = 3,
        retry_sleep_seconds: float = 1.0,
        request_delay_seconds: float = 0.0,
        page_size: int = 20,
    ):
        if session is None:
            import requests

            session = requests
        self.session = session
        self.retries = retries
        self.retry_sleep_seconds = retry_sleep_seconds
        self.request_delay_seconds = request_delay_seconds
        self.page_size = page_size
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://quote.eastmoney.com/center/gridlist.html",
        }

    def load_universe(self) -> list[dict[str, str]]:
        url = "https://82.push2.eastmoney.com/api/qt/clist/get"
        rows: list[dict[str, str]] = []
        page = 1
        while True:
            if self.request_delay_seconds and page > 1:
                time.sleep(self.request_delay_seconds)
            payload = self._get_page(url, page, self.page_size)
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

    def load_market_cap(self, symbol: str) -> dict[str, str]:
        rows = self.load_market_caps([symbol])
        if not rows:
            raise ValueError(f"No Eastmoney market cap found for {symbol}")
        return rows[0]

    def load_market_caps(self, symbols: list[str]) -> list[dict[str, str]]:
        if not symbols:
            return []
        payload = self._get_json(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            {
                "fltt": 2,
                "invt": 2,
                "fields": "f12,f13,f14,f20,f21",
                "secids": ",".join(self._secid(symbol) for symbol in symbols),
            },
        )
        code_to_symbol = {symbol.split(".")[0]: symbol for symbol in symbols}
        rows: list[dict[str, str]] = []
        for item in (payload.get("data") or {}).get("diff") or []:
            code = str(item.get("f12") or "")
            symbol = code_to_symbol.get(code)
            if not symbol:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "name": str(item.get("f14") or ""),
                    "exchange": self._exchange(symbol),
                    "market_cap": str(item.get("f20") or 0),
                    "status": "",
                }
            )
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

    def _secid(self, symbol: str) -> str:
        code = symbol.split(".")[0]
        suffix = symbol.split(".")[-1].upper() if "." in symbol else ""
        if suffix == "SH" or code.startswith(("60", "68", "90", "51", "52", "58")):
            return f"1.{code}"
        return f"0.{code}"

    def _exchange(self, symbol: str) -> str:
        if "." in symbol:
            return symbol.split(".")[-1].upper()
        code = symbol.split(".")[0]
        if code.startswith(("60", "68", "90", "51", "52", "58")):
            return "SH"
        if code.startswith(("43", "83", "87", "88", "92")):
            return "BJ"
        return "SZ"


class TencentStockUniverseProvider:
    def __init__(
        self,
        session: Any | None = None,
        retries: int = 3,
        retry_sleep_seconds: float = 1.0,
        page_size: int = 100,
    ):
        if session is None:
            import requests

            session = requests
        self.session = session
        self.retries = retries
        self.retry_sleep_seconds = retry_sleep_seconds
        self.page_size = page_size
        self.headers = {"User-Agent": "Mozilla/5.0"}

    def load_universe(self) -> list[dict[str, str]]:
        raise NotImplementedError("TencentStockUniverseProvider only supports market-cap refresh")

    def load_market_cap(self, symbol: str) -> dict[str, str]:
        rows = self.load_market_caps([symbol])
        if not rows:
            raise ValueError(f"No Tencent market cap found for {symbol}")
        return rows[0]

    def load_market_caps(self, symbols: list[str]) -> list[dict[str, str]]:
        if not symbols:
            return []
        payload = self._get_text(
            "https://qt.gtimg.cn/q=" + ",".join(self._q_symbol(symbol) for symbol in symbols)
        )
        q_to_symbol = {self._q_symbol(symbol): symbol for symbol in symbols}
        rows: list[dict[str, str]] = []
        for part in payload.split(";"):
            if not part.strip() or "=" not in part:
                continue
            variable, raw = part.split("=", 1)
            q_symbol = variable.replace("v_", "").strip()
            symbol = q_to_symbol.get(q_symbol)
            if not symbol:
                continue
            fields = raw.strip().strip('"').split("~")
            if len(fields) <= 45 or fields[45] in {"", "-"}:
                continue
            market_cap = Decimal(fields[45]) * Decimal("100000000")
            if market_cap <= 0:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "name": fields[1] if len(fields) > 1 else "",
                    "exchange": self._exchange(symbol),
                    "market_cap": str(int(market_cap)),
                    "status": "",
                }
            )
        return rows

    def _get_text(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.get(url, timeout=20, headers=self.headers)
                response.raise_for_status()
                return response.text
            except Exception as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    time.sleep(self.retry_sleep_seconds)
        raise RuntimeError(f"Tencent market-cap request failed after {self.retries} attempts: {last_error}") from last_error

    def _q_symbol(self, symbol: str) -> str:
        code = symbol.split(".")[0]
        suffix = symbol.split(".")[-1].upper() if "." in symbol else ""
        if suffix == "SH" or code.startswith(("60", "68", "90", "51", "52", "58")):
            return f"sh{code}"
        if suffix == "BJ" or code.startswith(("43", "83", "87", "88", "92")):
            return f"bj{code}"
        return f"sz{code}"

    def _exchange(self, symbol: str) -> str:
        if "." in symbol:
            return symbol.split(".")[-1].upper()
        code = symbol.split(".")[0]
        if code.startswith(("60", "68", "90", "51", "52", "58")):
            return "SH"
        if code.startswith(("43", "83", "87", "88", "92")):
            return "BJ"
        return "SZ"


class FallbackStockUniverseProvider:
    def __init__(self, primary: StockUniverseProvider, fallback: StockUniverseProvider | None = None):
        self.primary = primary
        self.fallback = fallback
        self.page_size = int(getattr(primary, "page_size", getattr(fallback, "page_size", 20)))

    def load_universe(self) -> list[dict[str, str]]:
        try:
            return self.primary.load_universe()
        except Exception:
            if self.fallback is None:
                raise
            return self.fallback.load_universe()

    def load_market_cap(self, symbol: str) -> dict[str, str]:
        try:
            return self.primary.load_market_cap(symbol)  # type: ignore[attr-defined]
        except Exception:
            if self.fallback is None:
                raise
            return self.fallback.load_market_cap(symbol)  # type: ignore[attr-defined]

    def load_market_caps(self, symbols: list[str]) -> list[dict[str, str]]:
        try:
            primary_rows = self.primary.load_market_caps(symbols)  # type: ignore[attr-defined]
        except Exception:
            if self.fallback is None:
                raise
            return self.fallback.load_market_caps(symbols)  # type: ignore[attr-defined]

        if self.fallback is None:
            return primary_rows

        found_symbols = {
            row.get("symbol", "")
            for row in primary_rows
            if row.get("symbol") and self._market_cap_value(row) > 0
        }
        missing_symbols = [symbol for symbol in symbols if symbol not in found_symbols]
        if not missing_symbols:
            return primary_rows
        fallback_rows = self.fallback.load_market_caps(missing_symbols)  # type: ignore[attr-defined]
        return primary_rows + fallback_rows

    def _market_cap_value(self, row: dict[str, str]) -> float:
        raw = row.get("market_cap") or row.get("total_market_cap") or "0"
        return float(str(raw).replace(",", ""))
