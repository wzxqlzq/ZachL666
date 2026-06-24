import csv
import json
import shutil
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TMP_ROOT = Path(__file__).resolve().parents[1] / "test_tmp"
TMP_ROOT.mkdir(exist_ok=True)

from market_data import AkshareMarketDataProvider, CsvMarketDataProvider, EastmoneyMarketDataProvider, FallbackMarketDataProvider
from main import apply_env_overrides, load_dotenv
from models import Bar, Position, Quote, Signal, StockCandidate
from notifier import EmailNotificationService, EmailNotifier, EmailSender, SelectionReport
from offline_data import OfflineDataStore, OfflineDataSync, OfflineMarketDataProvider
from portfolio import PortfolioRepository
from position_sizing import AtrPositionSizer
from runner import Runner
from run_weekly_update import run_weekly_update
from signal_store import SignalStore
from stock_selector import CsvStockSelector, RuleBasedStockSelector
from strategy import TurtleStrategyEngine
from trade_gateway import AlertTradeGateway
from universe_provider import (
    AkshareStockUniverseProvider,
    EastmoneyStockUniverseProvider,
    FallbackStockUniverseProvider,
    TencentStockUniverseProvider,
)


class FakeEmailSender(EmailSender):
    def __init__(self):
        self.messages = []

    def send(self, subject: str, body: str) -> None:
        self.messages.append((subject, body))


class FakeNotificationService:
    def __init__(self):
        self.selection_reports = []

    def send_selection_report(self, report: SelectionReport) -> None:
        self.selection_reports.append(report)


class FakeSelector:
    def __init__(self, candidates):
        self.candidates = candidates

    def select(self, as_of: date):
        return self.candidates


class FakeMarketData:
    def __init__(self, bars, quotes):
        self.bars = bars
        self.quotes = quotes
        self.index = 0

    def load_daily_bars(self, symbol: str, end_date: date | None = None):
        return self.bars

    def get_quote(self, symbol: str, at: datetime | None = None):
        quote = self.quotes[min(self.index, len(self.quotes) - 1)]
        self.index += 1
        return quote


class FakeUniverseProvider:
    def load_universe(self):
        return [
            {
                "symbol": "000001.SZ",
                "name": "Normal",
                "exchange": "SZ",
                "market_cap": "30000000000",
                "status": "",
            }
        ]


class FakeMarketCapUniverseProvider:
    def load_universe(self):
        return [
            {
                "symbol": "000001.SZ",
                "name": "Normal",
                "exchange": "SZ",
                "market_cap": "33000000000",
                "status": "",
            }
        ]

    def load_market_cap(self, symbol):
        return self.load_market_caps([symbol])[0]

    def load_market_caps(self, symbols):
        return [
            {
                "symbol": symbol,
                "name": "Normal",
                "exchange": symbol.split(".")[-1],
                "market_cap": "33000000000",
                "status": "",
            }
            for symbol in symbols
        ]


class FakeSingleMarketCapProvider:
    def __init__(self):
        self.calls = []

    def load_universe(self):
        return []

    def load_market_cap(self, symbol):
        self.calls.append(symbol)
        return {
            "symbol": symbol,
            "name": "",
            "exchange": symbol.split(".")[-1],
            "market_cap": "33000000000",
            "status": "",
        }


class CountingMarketCapProvider(FakeMarketCapUniverseProvider):
    def __init__(self):
        self.calls = []

    def load_market_caps(self, symbols):
        self.calls.append(list(symbols))
        return super().load_market_caps(symbols)


class MapMarketData:
    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol

    def load_daily_bars(self, symbol: str, end_date: date | None = None):
        return self.bars_by_symbol.get(symbol, [])

    def get_quote(self, symbol: str, at: datetime | None = None):
        bar = self.bars_by_symbol[symbol][-1]
        return Quote(symbol, at or datetime.combine(bar.trade_date, datetime.min.time()), bar.close)


class CountingMarketData:
    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol
        self.calls = []

    def load_daily_bars(self, symbol: str, end_date: date | None = None):
        self.calls.append(symbol)
        return self.bars_by_symbol.get(symbol, [])

    def get_quote(self, symbol: str, at: datetime | None = None):
        raise NotImplementedError


class FakeFrame:
    def __init__(self, records):
        self.records = records

    def to_dict(self, orient):
        self.orient = orient
        return self.records


class FailingProvider:
    def load_universe(self):
        raise RuntimeError("primary failed")

    def load_daily_bars(self, symbol: str, end_date: date | None = None):
        raise RuntimeError("primary failed")

    def get_quote(self, symbol: str, at: datetime | None = None):
        raise RuntimeError("primary failed")


class FakeAkshareClient:
    def stock_info_a_code_name(self):
        return FakeFrame(
            [
                {"code": "000001", "name": "Ping An"},
                {"code": "430001", "name": "Beijing"},
            ]
        )

    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust, timeout=None):
        self.hist_args = {
            "symbol": symbol,
            "period": period,
            "start_date": start_date,
            "end_date": end_date,
            "adjust": adjust,
        }
        return FakeFrame(
            [
                {
                    "日期": "2026-01-02",
                    "开盘": 9.0,
                    "最高": 10.0,
                    "最低": 8.0,
                    "收盘": 9.5,
                    "成交量": 100,
                    "成交额": 950000,
                }
            ]
        )


class FakeAkshareNoSpotClient(FakeAkshareClient):
    def stock_zh_a_spot_em(self):
        raise RuntimeError("spot unavailable")


class FakeAkshareHistFailDailyOkClient(FakeAkshareClient):
    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust, timeout=None):
        raise RuntimeError("hist unavailable")

    def stock_zh_a_daily(self, symbol, start_date, end_date, adjust):
        self.daily_args = {
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
            "adjust": adjust,
        }
        return FakeFrame(
            [
                {
                    "date": "2026-01-02",
                    "open": 9.0,
                    "high": 10.0,
                    "low": 8.0,
                    "close": 9.5,
                    "volume": 100,
                    "amount": 950000,
                }
            ]
        )


def _fake_akshare_spot_em(self):
    return FakeFrame(
        [
            {
                "symbol": "000001",
                "name": "Ping An",
                "price": 10.1,
                "open": 9.9,
                "high": 10.2,
                "low": 9.8,
                "volume": 200,
                "market_cap": 30000000000,
            },
            {
                "symbol": "430001",
                "name": "Beijing",
                "price": 5.0,
                "open": 5.0,
                "high": 5.1,
                "low": 4.9,
                "volume": 100,
                "market_cap": 5000000000,
            },
        ]
    )


FakeAkshareClient.stock_zh_a_spot_em = _fake_akshare_spot_em


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeEastmoneySession:
    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout, headers):
        self.calls.append({"url": url, "params": params, "timeout": timeout, "headers": headers})
        if "kline" in url:
            return FakeResponse(
                {
                    "rc": 0,
                    "data": {
                        "klines": [
                            "2026-01-02,9.00,9.50,10.00,8.00,100,950000,0,0,0,0",
                        ]
                    },
                }
            )
        return FakeResponse(
            {
                "rc": 0,
                "data": {
                    "f43": 1052,
                    "f44": 1077,
                    "f45": 1052,
                    "f46": 1074,
                    "f47": 1426893,
                    "f57": "000001",
                    "f58": "平安银行",
                },
            }
        )

    def stock_zh_a_spot_em(self):
        return FakeFrame(
            [
                {
                    "代码": "000001",
                    "名称": "Ping An",
                    "最新价": 10.1,
                    "今开": 9.9,
                    "最高": 10.2,
                    "最低": 9.8,
                    "成交量": 200,
                    "总市值": 30000000000,
                },
                {
                    "代码": "430001",
                    "名称": "Beijing",
                    "最新价": 5.0,
                    "今开": 5.0,
                    "最高": 5.1,
                    "最低": 4.9,
                    "成交量": 100,
                    "总市值": 5000000000,
                },
            ]
        )


class FakeEastmoneyUniverseSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout, headers):
        self.calls.append({"url": url, "params": params, "timeout": timeout, "headers": headers})
        if "ulist" in url:
            return FakeResponse(
                {
                    "rc": 0,
                    "data": {
                        "total": 1,
                        "diff": [
                            {
                                "f12": "000001",
                                "f13": 0,
                                "f14": "Ping An",
                                "f20": 33000000000,
                                "f21": 32000000000,
                            }
                        ],
                    },
                }
            )
        page = params["pn"]
        if page == 1:
            return FakeResponse(
                {
                    "rc": 0,
                    "data": {
                        "total": 3,
                        "diff": [
                            {"f12": "000001", "f13": 0, "f14": "Ping An", "f20": 33000000000},
                            {"f12": "600000", "f13": 1, "f14": "Pufa", "f20": 42000000000},
                        ],
                    },
                }
            )
        return FakeResponse(
            {
                "rc": 0,
                "data": {
                    "total": 3,
                    "diff": [
                        {"f12": "000002", "f13": 0, "f14": "Vanke", "f20": 21000000000},
                    ],
                },
            }
        )


class FakeTencentSession:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout, headers):
        self.calls.append({"url": url, "timeout": timeout, "headers": headers})
        return FakeTextResponse(
            'v_sz300009="51~安科生物~300009~7.67~7.67~7.66~190563~96337~94227~7.66~391~7.65~501~7.64~56~7.63~35~7.62~61~7.67~343~7.68~63~7.69~373~7.70~414~7.71~959~~20260622153318~0.00~0.00~7.68~7.35~7.67/190563/142533271~190563~14253~1.55~18.47~~7.68~7.35~4.30~94.36~128.14";'
            'v_bj920992="62~中科美菱~920992~12.79~12.65~12.55~14052~8165~5887~12.78~30~12.77~57~12.71~15~12.70~15~12.68~40~12.79~15~12.80~39~12.81~18~12.82~207~12.84~3~~20260622153502~0.14~1.11~12.88~12.08~12.79/14052/17473629~14052~1747.36~2.87~61.56~~12.88~12.08~6.32~6.27~12.37";'
        )


class FakeTextResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class FailingMarketCapProvider:
    page_size = 2

    def load_universe(self):
        return []

    def load_market_cap(self, symbol):
        raise RuntimeError("market cap unavailable")

    def load_market_caps(self, symbols):
        raise RuntimeError("market caps unavailable")


class PartialMarketCapProvider:
    page_size = 2

    def load_universe(self):
        return []

    def load_market_caps(self, symbols):
        return [
            {
                "symbol": symbols[0],
                "name": "",
                "exchange": symbols[0].split(".")[-1],
                "market_cap": "33000000000",
                "status": "",
            }
        ]


class WeeklyArgs:
    root = ""
    output = ""
    lookback_days = 14
    workers = 1
    limit = 0
    failures_csv = ""
    request_delay = 0.0
    max_retries = 0
    provider = "eastmoney"
    fallback = "none"
    akshare_timeout = 30
    akshare_history_source = "sina"
    market_cap_provider = "tencent"
    market_cap_fallback = "none"
    market_cap_page_size = 100
    force_market_cap_refresh = False
    target_date = "auto"
    target_probe_symbol = "000001.SZ"
    update_scope = "selection"
    bar_worker_mode = "serial"
    bar_workers = 1
    bar_batch_size = 20
    bar_timeout_seconds = 60
    final_retry_provider = "none"
    skip_up_to_date_bars = True
    skip_existing_market_cap = False
    skip_market_cap_refresh = False
    notify_selection = False


def make_bars(symbol="000001.SZ", count=20, high=10.0, low=8.0):
    start = date(2026, 1, 1)
    return [
        Bar(
            symbol=symbol,
            trade_date=start + timedelta(days=i),
            open=9.0,
            high=high,
            low=low,
            close=9.0,
            volume=1000000,
        )
        for i in range(count)
    ]


def make_completed_bars(symbol="000001.SZ", count=55, high=10.0, low=8.0, end=date(2026, 1, 1)):
    start = end - timedelta(days=count - 1)
    return [
        Bar(
            symbol=symbol,
            trade_date=start + timedelta(days=i),
            open=9.0,
            high=high,
            low=low,
            close=9.0,
            volume=1000000,
        )
        for i in range(count)
    ]


def make_atr_bars(symbol="000001.SZ", count=56, high=10.0, low=9.0, close=9.5, end=date(2026, 1, 1)):
    start = end - timedelta(days=count - 1)
    return [
        Bar(
            symbol=symbol,
            trade_date=start + timedelta(days=i),
            open=close,
            high=high,
            low=low,
            close=close,
            volume=1000000,
        )
        for i in range(count)
    ]


def make_selector_bars(
    symbol="000001.SZ",
    count=121,
    close=13.0,
    high=13.5,
    low=12.5,
    amount=600_000_000,
):
    start = date(2026, 1, 1)
    bars = []
    for i in range(count):
        if i < count - 50:
            base_close = 10.0
            bar_high = 10.5
            bar_low = 9.5
        elif i < count - 10:
            base_close = 12.0
            bar_high = 12.5
            bar_low = 11.8
        else:
            base_close = close
            bar_high = high
            bar_low = low
        bars.append(
            Bar(
                symbol=symbol,
                trade_date=start + timedelta(days=i),
                open=base_close,
                high=bar_high,
                low=bar_low,
                close=base_close,
                volume=1000000,
                amount=amount,
            )
        )
    return bars


def make_selector_bars_without_prior_breakout(symbol="000001.SZ"):
    bars = make_selector_bars(symbol)
    anchored = []
    for index, bar in enumerate(bars):
        anchored.append(
            Bar(
                symbol=bar.symbol,
                trade_date=bar.trade_date,
                open=bar.open,
                high=14.0 if index < 66 else bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                amount=bar.amount,
            )
        )
    return anchored


def make_selector_bars_after_ended_trend(symbol="000001.SZ"):
    start = date(2026, 1, 1)
    bars = []
    for index in range(180):
        if index < 60:
            close = 10.0
            high = 10.5
            low = 9.5
        elif index < 90:
            close = 12.0
            high = 12.5
            low = 11.5
        elif index == 90:
            close = 9.0
            high = 9.5
            low = 8.8
        elif index < 130:
            close = 10.0
            high = 20.0 if index == 100 else 10.5
            low = 9.5
        elif index < 170:
            close = 12.0
            high = 13.1 if index == 169 else 12.5
            low = 11.8
        else:
            close = 13.0
            high = 13.5
            low = 12.5
        bars.append(
            Bar(
                symbol=symbol,
                trade_date=start + timedelta(days=index),
                open=close,
                high=high,
                low=low,
                close=close,
                volume=1000000,
                amount=600_000_000,
            )
        )
    return bars


def case_dir(name: str) -> Path:
    path = TMP_ROOT / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


class StockSelectorTests(unittest.TestCase):
    def test_csv_selector_filters_invalid_rows(self):
        tmp = case_dir("selector")
        path = tmp / "candidates.csv"
        path.write_text(
            "symbol,name,reason,status\n"
            "000001.SZ,Ping An,manual,\n"
            ",Blank,manual,\n"
            "000002.SZ,ST Example,manual,\n"
            "000003.SZ,Normal,manual,suspended\n",
            encoding="utf-8",
        )

        result = CsvStockSelector(str(path)).select(date(2026, 1, 2))

        self.assertEqual([item.symbol for item in result], ["000001.SZ"])

    def test_rule_based_selector_accepts_stock_matching_all_rules(self):
        tmp = case_dir("rule_selector_pass")
        universe = tmp / "universe.csv"
        universe.write_text(
            "symbol,name,exchange,market_cap,status\n"
            "000001.SZ,Normal,SZ,30000000000,\n",
            encoding="utf-8",
        )

        selector = RuleBasedStockSelector(
            str(universe),
            MapMarketData({"000001.SZ": make_selector_bars_without_prior_breakout()}),
        )

        result = selector.select(date(2026, 5, 1))

        self.assertEqual([item.symbol for item in result], ["000001.SZ"])

    def test_rule_based_selector_filters_static_universe_rules(self):
        tmp = case_dir("rule_selector_static_filters")
        universe = tmp / "universe.csv"
        universe.write_text(
            "symbol,name,exchange,market_cap,status\n"
            "000001.SZ,Normal,SZ,30000000000,\n"
            "000002.SZ,ST Bad,SZ,30000000000,\n"
            "430001.BJ,Beijing,BJ,30000000000,\n"
            "000003.SZ,Small,SZ,10000000000,\n",
            encoding="utf-8",
        )
        bars = {
            "000001.SZ": make_selector_bars_without_prior_breakout("000001.SZ"),
            "000002.SZ": make_selector_bars_without_prior_breakout("000002.SZ"),
            "430001.BJ": make_selector_bars_without_prior_breakout("430001.BJ"),
            "000003.SZ": make_selector_bars_without_prior_breakout("000003.SZ"),
        }

        result = RuleBasedStockSelector(str(universe), MapMarketData(bars)).select(date(2026, 5, 1))

        self.assertEqual([item.symbol for item in result], ["000001.SZ"])

    def test_rule_based_selector_filters_low_amount_and_price_setup(self):
        tmp = case_dir("rule_selector_dynamic_filters")
        universe = tmp / "universe.csv"
        universe.write_text(
            "symbol,name,exchange,market_cap,status\n"
            "000001.SZ,LowAmount,SZ,30000000000,\n"
            "000002.SZ,TooHigh,SZ,30000000000,\n",
            encoding="utf-8",
        )
        too_high = make_selector_bars("000002.SZ", close=15.0, high=15.0, low=13.0)
        bars = {
            "000001.SZ": make_selector_bars("000001.SZ", amount=100_000_000),
            "000002.SZ": too_high,
        }

        result = RuleBasedStockSelector(str(universe), MapMarketData(bars)).select(date(2026, 5, 1))

        self.assertEqual(result, [])

    def test_rule_based_selector_filters_active_turtle_trend(self):
        tmp = case_dir("rule_selector_active_trend")
        universe = tmp / "universe.csv"
        universe.write_text(
            "symbol,name,exchange,market_cap,status\n"
            "000001.SZ,ActiveTrend,SZ,30000000000,\n",
            encoding="utf-8",
        )

        result = RuleBasedStockSelector(
            str(universe),
            MapMarketData({"000001.SZ": make_selector_bars("000001.SZ")}),
        ).select(date(2026, 5, 1))

        self.assertEqual(result, [])

    def test_rule_based_selector_allows_stock_after_20_day_breakdown_ends_trend(self):
        tmp = case_dir("rule_selector_ended_trend")
        universe = tmp / "universe.csv"
        universe.write_text(
            "symbol,name,exchange,market_cap,status\n"
            "000001.SZ,EndedTrend,SZ,30000000000,\n",
            encoding="utf-8",
        )

        result = RuleBasedStockSelector(
            str(universe),
            MapMarketData({"000001.SZ": make_selector_bars_after_ended_trend("000001.SZ")}),
        ).select(date(2026, 6, 30))

        self.assertEqual([item.symbol for item in result], ["000001.SZ"])

    def test_rule_based_selector_returns_selection_details(self):
        tmp = case_dir("rule_selector_details")
        universe = tmp / "universe.csv"
        universe.write_text(
            "symbol,name,exchange,market_cap,status\n"
            "000001.SZ,Waiting,SZ,30000000000,\n"
            "000002.SZ,Active,SZ,30000000000,\n",
            encoding="utf-8",
        )
        selector = RuleBasedStockSelector(
            str(universe),
            MapMarketData(
                {
                    "000001.SZ": make_selector_bars_without_prior_breakout("000001.SZ"),
                    "000002.SZ": make_selector_bars("000002.SZ"),
                }
            ),
        )

        details = selector.select_with_details(date(2026, 5, 1))

        self.assertEqual([item.symbol for item in details.before_trend_filter], ["000001.SZ", "000002.SZ"])
        self.assertEqual([item.symbol for item in details.selected], ["000001.SZ"])
        self.assertEqual([item.symbol for item in details.excluded_by_active_trend], ["000002.SZ"])
        self.assertEqual(details.excluded_by_active_trend[0].reason, "active_turtle_trend")


class MarketDataTests(unittest.TestCase):
    def test_csv_provider_returns_standard_bars_and_quote(self):
        tmp = case_dir("market_data")
        path = tmp / "daily.csv"
        path.write_text(
            "date,symbol,open,high,low,close,volume\n"
            "2026-01-02,000001.SZ,9,10,8,9.5,100\n",
            encoding="utf-8",
        )

        provider = CsvMarketDataProvider(str(path))
        bars = provider.load_daily_bars("000001.SZ")
        quote = provider.get_quote("000001.SZ", at=datetime(2026, 1, 2, 10, 0))

        self.assertEqual(bars[0].symbol, "000001.SZ")
        self.assertEqual(quote.price, 9.5)

    def test_csv_provider_raises_when_quote_missing(self):
        tmp = case_dir("market_data_missing")
        path = tmp / "daily.csv"
        path.write_text("date,symbol,open,high,low,close,volume\n", encoding="utf-8")
        provider = CsvMarketDataProvider(str(path))

        with self.assertRaises(ValueError):
            provider.get_quote("000001.SZ")

    def test_akshare_provider_maps_history_quote_and_universe(self):
        tmp = case_dir("akshare_provider")
        client = FakeAkshareClient()
        provider = AkshareMarketDataProvider(lookback_days=30, adjust="qfq", ak_client=client)

        bars = provider.load_daily_bars("000001.SZ", end_date=date(2026, 1, 10))
        quote = provider.get_quote("000001.SZ", at=datetime(2026, 1, 10, 10, 0))
        count = provider.export_stock_universe_csv(str(tmp / "stock_universe.csv"))

        self.assertEqual(client.hist_args["symbol"], "000001")
        self.assertEqual(bars[0].amount, 950000)
        self.assertEqual(quote.price, 10.1)
        self.assertEqual(count, 2)

        with (tmp / "stock_universe.csv").open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["symbol"], "000001.SZ")
        self.assertEqual(rows[1]["symbol"], "430001.BJ")

    def test_akshare_provider_falls_back_to_sina_daily_history(self):
        client = FakeAkshareHistFailDailyOkClient()
        provider = AkshareMarketDataProvider(lookback_days=30, adjust="qfq", ak_client=client)

        bars = provider.load_daily_bars("000001.SZ", end_date=date(2026, 1, 10))

        self.assertEqual(client.daily_args["symbol"], "sz000001")
        self.assertEqual(bars[0].symbol, "000001.SZ")
        self.assertEqual(bars[0].amount, 950000)

    def test_eastmoney_provider_maps_direct_history_and_quote(self):
        session = FakeEastmoneySession()
        provider = EastmoneyMarketDataProvider(lookback_days=30, adjust="qfq", session=session)

        bars = provider.load_daily_bars("000001.SZ", end_date=date(2026, 1, 10))
        quote = provider.get_quote("000001.SZ", at=datetime(2026, 1, 10, 10, 0))

        self.assertEqual(session.calls[0]["params"]["secid"], "0.000001")
        self.assertEqual(session.calls[1]["params"]["secid"], "0.000001")
        self.assertEqual(bars[0].amount, 950000)
        self.assertEqual(quote.price, 10.52)
        self.assertEqual(quote.open, 10.74)

    def test_offline_store_merges_bars_and_serves_provider(self):
        tmp = case_dir("offline_store")
        store = OfflineDataStore(str(tmp / "offline"))
        first = [
            Bar("000001.SZ", date(2026, 1, 1), 9, 10, 8, 9.5, 100, 950000),
            Bar("000001.SZ", date(2026, 1, 2), 10, 11, 9, 10.5, 200, 2100000),
        ]
        second = [
            Bar("000001.SZ", date(2026, 1, 2), 10, 12, 9, 11.5, 300, 3450000),
            Bar("000001.SZ", date(2026, 1, 3), 11, 12, 10, 11.8, 400, 4720000),
        ]

        store.save_bars("000001.SZ", first)
        store.save_bars("000001.SZ", second)
        bars = OfflineMarketDataProvider(store).load_daily_bars("000001.SZ")

        self.assertEqual([bar.trade_date for bar in bars], [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)])
        self.assertEqual(bars[1].close, 11.5)
        self.assertEqual(store.latest_bar_date("000001.SZ"), date(2026, 1, 3))

    def test_offline_sync_saves_universe_and_symbol_bars(self):
        tmp = case_dir("offline_sync")
        store = OfflineDataStore(str(tmp / "offline"))
        market_data = CountingMarketData({"000001.SZ": make_selector_bars("000001.SZ")})
        sync = OfflineDataSync(
            store=store,
            universe_provider=FakeUniverseProvider(),
            market_data_provider=market_data,
            workers=1,
        )

        universe_count = sync.sync_universe()
        success_count, failures = sync.sync_bars()

        self.assertEqual(universe_count, 1)
        self.assertEqual(success_count, 1)
        self.assertEqual(failures, [])
        self.assertEqual(market_data.calls, ["000001.SZ"])
        self.assertEqual(len(store.load_bars("000001.SZ")), 121)

    def test_offline_sync_can_refresh_market_caps_without_touching_bars(self):
        tmp = case_dir("offline_market_caps")
        store = OfflineDataStore(str(tmp / "offline"))
        store.save_universe(
            [
                {
                    "symbol": "000001.SZ",
                    "name": "Normal",
                    "exchange": "SZ",
                    "market_cap": "0",
                    "status": "",
                }
            ]
        )
        store.save_bars("000001.SZ", make_selector_bars("000001.SZ"))
        sync = OfflineDataSync(
            store=store,
            universe_provider=FakeMarketCapUniverseProvider(),
            market_data_provider=CountingMarketData({}),
            workers=1,
        )

        updated_count, failures = sync.sync_market_caps()

        self.assertEqual(updated_count, 1)
        self.assertEqual(failures, [])
        self.assertEqual(store.load_universe()[0]["market_cap"], "33000000000")
        self.assertEqual(len(store.load_bars("000001.SZ")), 121)

    def test_offline_sync_market_caps_can_skip_existing(self):
        tmp = case_dir("offline_market_caps_skip_existing")
        store = OfflineDataStore(str(tmp / "offline"))
        store.save_universe(
            [
                {"symbol": "000001.SZ", "name": "A", "exchange": "SZ", "market_cap": "0", "status": ""},
                {"symbol": "000002.SZ", "name": "B", "exchange": "SZ", "market_cap": "22000000000", "status": ""},
            ]
        )
        provider = FakeSingleMarketCapProvider()
        sync = OfflineDataSync(
            store=store,
            universe_provider=provider,
            market_data_provider=CountingMarketData({}),
            workers=1,
        )

        updated_count, failures = sync.sync_market_caps(skip_existing=True)

        self.assertEqual(updated_count, 1)
        self.assertEqual(failures, [])
        self.assertEqual(provider.calls, ["000001.SZ"])
        self.assertEqual(store.load_universe()[1]["market_cap"], "22000000000")

    def test_rule_selector_can_use_offline_provider(self):
        tmp = case_dir("offline_selection")
        store = OfflineDataStore(str(tmp / "offline"))
        store.save_universe(FakeUniverseProvider().load_universe())
        store.save_bars("000001.SZ", make_selector_bars_without_prior_breakout("000001.SZ"))

        selector = RuleBasedStockSelector(
            universe_csv_path=str(store.universe_path),
            market_data=OfflineMarketDataProvider(store),
        )

        result = selector.select(date(2026, 5, 1))

        self.assertEqual([item.symbol for item in result], ["000001.SZ"])

    def test_weekly_update_reuses_offline_universe_and_writes_selection(self):
        tmp = case_dir("weekly_update")
        root = tmp / "offline"
        output = tmp / "weekly.csv"
        store = OfflineDataStore(str(root))
        store.save_universe(FakeUniverseProvider().load_universe())
        store.save_bars("000001.SZ", make_selector_bars("000001.SZ", count=120))

        args = WeeklyArgs()
        args.root = str(root)
        args.output = str(output)
        args.limit = 1
        args.failures_csv = str(tmp / "failures.csv")
        args.target_date = "2026-05-01"

        with (
            patch("run_weekly_update.build_market_data_provider", return_value=CountingMarketData({"000001.SZ": make_selector_bars("000001.SZ")})),
            patch("run_weekly_update.build_universe_provider", return_value=FakeMarketCapUniverseProvider()),
        ):
            bar_count, bar_failures, market_cap_count, market_cap_failures, output_path = run_weekly_update(args)

        self.assertEqual(bar_count, 1)
        self.assertEqual(bar_failures, 0)
        self.assertEqual(market_cap_count, 1)
        self.assertEqual(market_cap_failures, 0)
        self.assertEqual(output_path, output)
        self.assertTrue(output.exists())
        self.assertEqual(store.load_universe()[0]["symbol"], "000001.SZ")
        self.assertGreater(len(store.load_bars("000001.SZ")), 120)

    def test_weekly_update_does_not_send_selection_email_by_default(self):
        tmp = case_dir("weekly_update_no_notify")
        root = tmp / "offline"
        output = tmp / "weekly.csv"
        store = OfflineDataStore(str(root))
        store.save_universe(FakeUniverseProvider().load_universe())
        store.save_bars("000001.SZ", make_selector_bars_without_prior_breakout("000001.SZ"))

        args = WeeklyArgs()
        args.root = str(root)
        args.output = str(output)
        args.target_date = "2026-05-01"
        args.failures_csv = str(tmp / "failures.csv")
        args.skip_market_cap_refresh = True

        with (
            patch("run_weekly_update.build_market_data_provider", return_value=CountingMarketData({})),
            patch("run_weekly_update.build_universe_provider", return_value=FakeMarketCapUniverseProvider()),
            patch("run_weekly_update.build_notification_service") as build_notification_service,
        ):
            run_weekly_update(args)

        build_notification_service.assert_not_called()

    def test_weekly_update_can_send_selection_email(self):
        tmp = case_dir("weekly_update_notify")
        root = tmp / "offline"
        output = tmp / "weekly.csv"
        store = OfflineDataStore(str(root))
        store.save_universe(
            [
                {"symbol": "000001.SZ", "name": "Waiting", "exchange": "SZ", "market_cap": "30000000000", "status": ""},
                {"symbol": "000002.SZ", "name": "Active", "exchange": "SZ", "market_cap": "30000000000", "status": ""},
            ]
        )
        store.save_bars("000001.SZ", make_selector_bars_without_prior_breakout("000001.SZ"))
        store.save_bars("000002.SZ", make_selector_bars("000002.SZ"))
        notifier = FakeNotificationService()

        args = WeeklyArgs()
        args.root = str(root)
        args.output = str(output)
        args.target_date = "2026-05-01"
        args.failures_csv = str(tmp / "failures.csv")
        args.skip_market_cap_refresh = True
        args.notify_selection = True

        with (
            patch("run_weekly_update.build_market_data_provider", return_value=CountingMarketData({})),
            patch("run_weekly_update.build_universe_provider", return_value=FakeMarketCapUniverseProvider()),
            patch("run_weekly_update.build_notification_service", return_value=notifier),
        ):
            run_weekly_update(args)

        self.assertEqual(len(notifier.selection_reports), 1)
        report = notifier.selection_reports[0]
        self.assertEqual([item.symbol for item in report.before_trend_filter], ["000001.SZ", "000002.SZ"])
        self.assertEqual([item.symbol for item in report.selected], ["000001.SZ"])
        self.assertEqual([item.symbol for item in report.excluded_by_active_trend], ["000002.SZ"])
        self.assertEqual(report.output_path, output)

    def test_weekly_update_skips_up_to_date_bars(self):
        tmp = case_dir("weekly_update_skip_latest")
        root = tmp / "offline"
        output = tmp / "weekly.csv"
        store = OfflineDataStore(str(root))
        store.save_universe(FakeUniverseProvider().load_universe())
        store.save_bars("000001.SZ", make_selector_bars("000001.SZ"))
        market_data = CountingMarketData({"000001.SZ": make_selector_bars("000001.SZ")})

        args = WeeklyArgs()
        args.root = str(root)
        args.output = str(output)
        args.target_date = "2026-05-01"
        args.failures_csv = str(tmp / "failures.csv")

        with (
            patch("run_weekly_update.build_market_data_provider", return_value=market_data),
            patch("run_weekly_update.build_universe_provider", return_value=FakeMarketCapUniverseProvider()),
        ):
            bar_count, bar_failures, market_cap_count, market_cap_failures, _ = run_weekly_update(args)

        self.assertEqual(bar_count, 0)
        self.assertEqual(bar_failures, 0)
        self.assertEqual(market_cap_count, 1)
        self.assertEqual(market_cap_failures, 0)
        self.assertEqual(market_data.calls, [])

    def test_weekly_update_skips_fresh_market_caps(self):
        tmp = case_dir("weekly_update_skip_market_caps")
        root = tmp / "offline"
        output = tmp / "weekly.csv"
        store = OfflineDataStore(str(root))
        store.save_universe(
            [
                {
                    "symbol": "000001.SZ",
                    "name": "Normal",
                    "exchange": "SZ",
                    "market_cap": "30000000000",
                    "status": "",
                    "updated_at": "2026-05-01",
                }
            ]
        )
        store.save_bars("000001.SZ", make_selector_bars("000001.SZ"))
        market_data = CountingMarketData({"000001.SZ": make_selector_bars("000001.SZ")})
        market_caps = CountingMarketCapProvider()

        args = WeeklyArgs()
        args.root = str(root)
        args.output = str(output)
        args.target_date = "2026-05-01"
        args.failures_csv = str(tmp / "failures.csv")

        with (
            patch("run_weekly_update.build_market_data_provider", return_value=market_data),
            patch("run_weekly_update.build_universe_provider", return_value=market_caps),
        ):
            _bar_count, _bar_failures, market_cap_count, market_cap_failures, _ = run_weekly_update(args)

        self.assertEqual(market_cap_count, 0)
        self.assertEqual(market_cap_failures, 0)
        self.assertEqual(market_caps.calls, [])

    def test_weekly_update_auto_target_uses_local_majority_date(self):
        tmp = case_dir("weekly_update_local_target")
        root = tmp / "offline"
        output = tmp / "weekly.csv"
        store = OfflineDataStore(str(root))
        store.save_universe(FakeUniverseProvider().load_universe())
        store.save_bars("000001.SZ", make_selector_bars("000001.SZ"))
        market_data = CountingMarketData({"000001.SZ": make_selector_bars("000001.SZ")})

        args = WeeklyArgs()
        args.root = str(root)
        args.output = str(output)
        args.target_date = "auto"
        args.failures_csv = str(tmp / "failures.csv")

        with (
            patch("run_weekly_update.build_market_data_provider", return_value=market_data),
            patch("run_weekly_update.build_universe_provider", return_value=FakeMarketCapUniverseProvider()),
        ):
            bar_count, bar_failures, _market_cap_count, _market_cap_failures, _ = run_weekly_update(args)

        self.assertEqual(bar_count, 0)
        self.assertEqual(bar_failures, 0)
        self.assertEqual(market_data.calls, [])

    def test_weekly_update_selection_scope_filters_static_and_market_cap_rules(self):
        tmp = case_dir("weekly_update_selection_scope")
        root = tmp / "offline"
        output = tmp / "weekly.csv"
        store = OfflineDataStore(str(root))
        store.save_universe(
            [
                {"symbol": "000001.SZ", "name": "Normal", "exchange": "SZ", "market_cap": "30000000000", "status": ""},
                {"symbol": "000002.SZ", "name": "Small", "exchange": "SZ", "market_cap": "10000000000", "status": ""},
                {"symbol": "000003.SZ", "name": "ST Bad", "exchange": "SZ", "market_cap": "30000000000", "status": ""},
                {"symbol": "430001.BJ", "name": "Beijing", "exchange": "BJ", "market_cap": "30000000000", "status": ""},
            ]
        )
        market_data = CountingMarketData({"000001.SZ": make_selector_bars("000001.SZ")})

        args = WeeklyArgs()
        args.root = str(root)
        args.output = str(output)
        args.target_date = "2026-05-01"
        args.failures_csv = str(tmp / "failures.csv")
        args.skip_market_cap_refresh = True

        with patch("run_weekly_update.build_market_data_provider", return_value=market_data):
            bar_count, bar_failures, _market_cap_count, _market_cap_failures, _ = run_weekly_update(args)

        self.assertEqual(bar_count, 1)
        self.assertEqual(bar_failures, 0)
        self.assertEqual(market_data.calls, ["000001.SZ"])

    def test_fallback_market_data_provider_uses_fallback_on_failure(self):
        fallback = CountingMarketData({"000001.SZ": make_selector_bars("000001.SZ")})
        provider = FallbackMarketDataProvider(FailingProvider(), fallback)

        bars = provider.load_daily_bars("000001.SZ")

        self.assertEqual(len(bars), 121)
        self.assertEqual(fallback.calls, ["000001.SZ"])

    def test_akshare_universe_provider_maps_rows(self):
        provider = AkshareStockUniverseProvider(ak_client=FakeAkshareClient())

        rows = provider.load_universe()

        self.assertEqual(rows[0]["symbol"], "000001.SZ")
        self.assertEqual(rows[0]["market_cap"], "30000000000")
        self.assertEqual(rows[1]["symbol"], "430001.BJ")

    def test_akshare_universe_provider_can_skip_market_cap_when_spot_fails(self):
        provider = AkshareStockUniverseProvider(ak_client=FakeAkshareNoSpotClient())

        rows = provider.load_universe()

        self.assertEqual(rows[0]["symbol"], "000001.SZ")
        self.assertEqual(rows[0]["name"], "Ping An")
        self.assertEqual(rows[0]["market_cap"], "0")

    def test_eastmoney_universe_provider_paginates_market_caps(self):
        session = FakeEastmoneyUniverseSession()
        provider = EastmoneyStockUniverseProvider(session=session, page_size=2)

        rows = provider.load_universe()

        self.assertEqual([row["symbol"] for row in rows], ["000001.SZ", "600000.SH", "000002.SZ"])
        self.assertEqual(rows[0]["market_cap"], "33000000000")
        self.assertEqual([call["params"]["pz"] for call in session.calls], [2, 2])

    def test_eastmoney_universe_provider_loads_single_stock_market_cap(self):
        session = FakeEastmoneyUniverseSession()
        provider = EastmoneyStockUniverseProvider(session=session)

        row = provider.load_market_cap("000001.SZ")

        self.assertEqual(row["symbol"], "000001.SZ")
        self.assertEqual(row["market_cap"], "33000000000")
        self.assertEqual(session.calls[0]["params"]["secids"], "0.000001")

    def test_tencent_universe_provider_loads_market_caps(self):
        session = FakeTencentSession()
        provider = TencentStockUniverseProvider(session=session)

        rows = provider.load_market_caps(["300009.SZ", "920992.BJ"])

        self.assertEqual([row["symbol"] for row in rows], ["300009.SZ", "920992.BJ"])
        self.assertEqual(rows[0]["market_cap"], "12814000000")
        self.assertEqual(rows[1]["market_cap"], "1237000000")
        self.assertIn("sz300009,bj920992", session.calls[0]["url"])

    def test_fallback_universe_provider_uses_market_cap_fallback_on_failure(self):
        fallback = TencentStockUniverseProvider(session=FakeTencentSession())
        provider = FallbackStockUniverseProvider(FailingMarketCapProvider(), fallback)

        rows = provider.load_market_caps(["300009.SZ"])

        self.assertEqual(rows[0]["symbol"], "300009.SZ")
        self.assertEqual(rows[0]["market_cap"], "12814000000")

    def test_fallback_universe_provider_fills_missing_market_caps(self):
        fallback = TencentStockUniverseProvider(session=FakeTencentSession())
        provider = FallbackStockUniverseProvider(PartialMarketCapProvider(), fallback)

        rows = provider.load_market_caps(["000001.SZ", "300009.SZ"])

        self.assertEqual([row["symbol"] for row in rows], ["000001.SZ", "300009.SZ"])
        self.assertEqual(rows[0]["market_cap"], "33000000000")
        self.assertEqual(rows[1]["market_cap"], "12814000000")

    def test_fallback_universe_provider_uses_fallback_on_failure(self):
        fallback = FakeUniverseProvider()
        provider = FallbackStockUniverseProvider(FailingProvider(), fallback)

        rows = provider.load_universe()

        self.assertEqual(rows[0]["symbol"], "000001.SZ")


class StrategyTests(unittest.TestCase):
    def test_atr_position_sizer_rounds_to_a_share_lot_size(self):
        size = AtrPositionSizer(
            account_equity=100_000,
            risk_per_trade=0.01,
            lot_size=100,
            stop_atr_multiple=2,
        ).size(price=20.0, atr=0.8)

        self.assertEqual(size.risk_amount, 1000)
        self.assertEqual(size.shares, 600)
        self.assertEqual(size.notional, 12000)
        self.assertAlmostEqual(size.stop_loss, 18.4)

    def test_buy_breakout_fires_immediately_above_55_day_high(self):
        strategy = TurtleStrategyEngine(
            55,
            20,
            20,
            0.01,
            confirm_minutes=0,
            account_equity=100_000,
        )
        candidate = StockCandidate("000001.SZ")
        bars = make_atr_bars(count=56, high=10.0, low=9.0, close=9.5)
        start = datetime(2026, 1, 2, 10, 0)

        first = strategy.on_quote(candidate, bars, Quote("000001.SZ", start, 10.5), Position("000001.SZ"))
        duplicate = strategy.on_quote(
            candidate,
            bars,
            Quote("000001.SZ", start + timedelta(minutes=1), 10.8),
            Position("000001.SZ"),
        )

        self.assertEqual(first[0].action, "BUY")
        self.assertIn("55-day high", first[0].reason)
        self.assertEqual(first[0].suggested_shares, 500)
        self.assertEqual(first[0].suggested_notional, 5250)
        self.assertAlmostEqual(first[0].atr, 1.0)
        self.assertAlmostEqual(first[0].stop_loss, 8.5)
        self.assertEqual(duplicate, [])

    def test_buy_signal_ignores_current_day_bar(self):
        strategy = TurtleStrategyEngine(55, 20, 20, 0.01, confirm_minutes=0)
        candidate = StockCandidate("000001.SZ")
        bars = make_atr_bars(count=56, high=10.0, low=9.0, close=9.5)
        bars.append(
            Bar(
                symbol="000001.SZ",
                trade_date=date(2026, 1, 2),
                open=9.0,
                high=12.0,
                low=9.0,
                close=11.0,
                volume=1000000,
            )
        )
        start = datetime(2026, 1, 2, 10, 0)

        signals = strategy.on_quote(candidate, bars, Quote("000001.SZ", start, 10.5), Position("000001.SZ"))

        self.assertEqual(signals[0].action, "BUY")

    def test_no_buy_signal_without_55_day_breakout(self):
        strategy = TurtleStrategyEngine(55, 20, 20, 0.01, confirm_minutes=0)
        candidate = StockCandidate("000001.SZ")
        bars = make_atr_bars(count=56, high=10.0, low=9.0, close=9.5)
        start = datetime(2026, 1, 2, 10, 0)

        signals = strategy.on_quote(candidate, bars, Quote("000001.SZ", start, 10.0), Position("000001.SZ"))

        self.assertEqual(signals, [])

    def test_sell_signal_for_strategy_position_below_20_day_low(self):
        strategy = TurtleStrategyEngine(55, 20, 20, 0.01, confirm_minutes=0)
        candidate = StockCandidate("000001.SZ")
        bars = make_atr_bars(count=56, high=10.0, low=8.0, close=9.5)
        start = datetime(2026, 1, 2, 10, 0)
        position = Position("000001.SZ", strategy_status="LONG")

        signals = strategy.on_quote(candidate, bars, Quote("000001.SZ", start, 7.5), position)

        self.assertEqual(signals[0].action, "SELL")
        self.assertIn("20-day low", signals[0].reason)

    def test_sell_signal_for_strategy_position_at_fixed_stop_loss(self):
        strategy = TurtleStrategyEngine(55, 20, 20, 0.01, confirm_minutes=0)
        candidate = StockCandidate("000001.SZ")
        bars = make_atr_bars(count=56, high=10.0, low=8.0, close=9.5)
        start = datetime(2026, 1, 2, 10, 0)
        position = Position("000001.SZ", strategy_status="LONG", strategy_stop_loss=8.5)

        signals = strategy.on_quote(candidate, bars, Quote("000001.SZ", start, 8.5), position)

        self.assertEqual(signals[0].action, "SELL")
        self.assertIn("fixed stop loss", signals[0].reason)

    def test_no_sell_signal_without_20_day_breakdown(self):
        strategy = TurtleStrategyEngine(55, 20, 20, 0.01, confirm_minutes=0)
        candidate = StockCandidate("000001.SZ")
        bars = make_atr_bars(count=56, high=10.0, low=8.0, close=9.5)
        start = datetime(2026, 1, 2, 10, 0)
        position = Position("000001.SZ", strategy_status="LONG")

        signals = strategy.on_quote(candidate, bars, Quote("000001.SZ", start, 8.0), position)

        self.assertEqual(signals, [])


class ConfigEnvTests(unittest.TestCase):
    def test_load_dotenv_reads_values_without_overriding_existing_environment(self):
        tmp = case_dir("dotenv")
        env_path = tmp / ".env"
        env_path.write_text(
            "\n".join(
                [
                    "SMTP_HOST=smtp.local",
                    'SMTP_PASSWORD="from_file"',
                    "SMTP_USERNAME=from_file@example.com",
                ]
            ),
            encoding="utf-8",
        )

        with patch.dict(
            "os.environ",
            {"SMTP_USERNAME": "from_environment@example.com"},
            clear=True,
        ):
            load_dotenv(str(env_path))

            import os

            self.assertEqual("smtp.local", os.environ["SMTP_HOST"])
            self.assertEqual("from_file", os.environ["SMTP_PASSWORD"])
            self.assertEqual("from_environment@example.com", os.environ["SMTP_USERNAME"])

    def test_apply_env_overrides_updates_email_config(self):
        config = {"email": {"smtp_host": "smtp.example.com", "recipients": ["old@example.com"]}}

        with patch.dict(
            "os.environ",
            {
                "SMTP_HOST": "smtp.local",
                "SMTP_PORT": "587",
                "SMTP_USERNAME": "user@example.com",
                "SMTP_PASSWORD": "secret",
                "SMTP_SENDER": "sender@example.com",
                "SMTP_RECIPIENTS": "one@example.com, two@example.com",
            },
            clear=True,
        ):
            apply_env_overrides(config)

        self.assertEqual("smtp.local", config["email"]["smtp_host"])
        self.assertEqual(587, config["email"]["smtp_port"])
        self.assertEqual("user@example.com", config["email"]["username"])
        self.assertEqual("secret", config["email"]["password"])
        self.assertEqual("sender@example.com", config["email"]["sender"])
        self.assertEqual(["one@example.com", "two@example.com"], config["email"]["recipients"])

    def test_run_loop_uses_interval_between_scans(self):
        import main as main_module

        class FakeRunner:
            portfolio_repository = None

            def __init__(self):
                self.calls = 0

            def run_once(self, as_of=None, at=None):
                self.calls += 1
                return 0

        runner = FakeRunner()
        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            raise KeyboardInterrupt

        with (
            patch("main.prepare_runner", return_value=runner),
            patch("main.time.sleep", side_effect=fake_sleep),
        ):
            with self.assertRaises(KeyboardInterrupt):
                main_module.run_loop(dry_run=True, interval_seconds=60)

        self.assertEqual(runner.calls, 1)
        self.assertEqual(sleep_calls, [60])


class EmailNotificationTests(unittest.TestCase):
    def test_selection_report_email_contains_before_after_and_excluded_lists(self):
        sender = FakeEmailSender()
        service = EmailNotificationService(sender)
        report = SelectionReport(
            as_of=date(2026, 6, 22),
            output_path=Path("selection_results/weekly_2026-06-22.csv"),
            before_trend_filter=[
                StockCandidate("000001.SZ", "Before"),
                StockCandidate("000002.SZ", "Active"),
            ],
            selected=[StockCandidate("000001.SZ", "Before")],
            excluded_by_active_trend=[StockCandidate("000002.SZ", "Active", "active_turtle_trend")],
        )

        service.send_selection_report(report)

        subject, body = sender.messages[0]
        self.assertIn("[选股报告] 2026-06-22 初筛=2 入选=1", subject)
        self.assertIn("趋势过滤前: 2", body)
        self.assertIn("趋势过滤后入选: 1", body)
        self.assertIn("因已有海龟趋势排除: 1", body)
        self.assertIn("000001.SZ Before", body)
        self.assertIn("000002.SZ Active", body)

    def test_selection_report_email_handles_empty_lists(self):
        sender = FakeEmailSender()
        service = EmailNotificationService(sender)
        report = SelectionReport(
            as_of=date(2026, 6, 22),
            output_path=Path("selection_results/weekly_2026-06-22.csv"),
            before_trend_filter=[],
            selected=[],
            excluded_by_active_trend=[],
        )

        service.send_selection_report(report)

        self.assertIn("(无)", sender.messages[0][1])

    def test_trade_signal_email_uses_common_notification_service(self):
        sender = FakeEmailSender()
        service = EmailNotificationService(sender)
        signal = Signal(
            symbol="000001.SZ",
            action="BUY",
            trade_date=date(2026, 1, 2),
            price=10.5,
            reason="breakout",
            risk_note="review",
            suggested_shares=1200,
            suggested_notional=24000,
            atr=0.8,
            stop_loss=18.4,
            risk_amount=1000,
        )

        service.send_trade_signal(signal)

        subject, body = sender.messages[0]
        self.assertIn("[交易提醒] BUY 000001.SZ 2026-01-02", subject)
        self.assertIn("触发原因: breakout", body)
        self.assertNotIn("风险提示:", body)
        self.assertIn("建议股数: 1200", body)
        self.assertIn("ATR: 0.8000", body)


class TradeGatewayTests(unittest.TestCase):
    def test_gateway_sends_email_and_writes_order_csv_once(self):
        tmp = case_dir("trade_gateway")
        sender = FakeEmailSender()
        gateway = AlertTradeGateway(
            notifier=EmailNotifier(sender),
            signal_store=SignalStore(str(tmp / "signals.json")),
            orders_dir=str(tmp / "orders"),
        )
        signal = Signal(
            symbol="000001.SZ",
            action="BUY",
            trade_date=date(2026, 1, 2),
            price=10.5,
            reason="test",
            risk_note="review",
            confirmed_at=datetime(2026, 1, 2, 10, 5),
            suggested_shares=1200,
            suggested_notional=12600,
            atr=0.8,
            stop_loss=8.9,
            risk_amount=1000,
        )

        first = gateway.submit_signal(signal)
        second = gateway.submit_signal(signal)

        order_file = tmp / "orders" / "orders_2026-01-02.csv"
        with order_file.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        self.assertEqual(first.status, "NEW")
        self.assertEqual(second.status, "DUPLICATE")
        self.assertEqual(len(sender.messages), 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "000001.SZ")
        self.assertEqual(rows[0]["suggested_shares"], "1200")
        self.assertEqual(rows[0]["suggested_notional"], "12600.00")
        self.assertEqual(rows[0]["atr"], "0.8000")
        self.assertEqual(rows[0]["stop_loss"], "8.9000")


class PortfolioRepositoryTests(unittest.TestCase):
    def test_portfolio_repository_loads_legacy_positions(self):
        tmp = case_dir("portfolio_legacy")
        path = tmp / "portfolio.json"
        path.write_text(
            json.dumps(
                {
                    "cash": 100000,
                    "positions": {
                        "000001.SZ": {
                            "shares": 0,
                            "avg_cost": 0,
                            "buy_date": None,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        positions = PortfolioRepository(str(path)).load_positions()

        self.assertEqual(positions["000001.SZ"].strategy_status, "FLAT")
        self.assertIsNone(positions["000001.SZ"].strategy_entry_date)
        self.assertIsNone(positions["000001.SZ"].strategy_stop_loss)

    def test_portfolio_repository_applies_buy_and_sell_strategy_state(self):
        tmp = case_dir("portfolio_strategy_state")
        path = tmp / "portfolio.json"
        path.write_text(
            json.dumps({"cash": 100000, "positions": {}}),
            encoding="utf-8",
        )
        repository = PortfolioRepository(str(path))
        buy = Signal(
            symbol="000001.SZ",
            action="BUY",
            trade_date=date(2026, 1, 2),
            price=10.5,
            reason="breakout",
            stop_loss=8.5,
        )
        sell = Signal(
            symbol="000001.SZ",
            action="SELL",
            trade_date=date(2026, 1, 9),
            price=8.0,
            reason="breakdown",
        )

        bought = repository.apply_strategy_signal(buy)
        sold = repository.apply_strategy_signal(sell)
        raw = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(bought.strategy_status, "LONG")
        self.assertEqual(bought.strategy_entry_date, date(2026, 1, 2))
        self.assertEqual(bought.strategy_entry_price, 10.5)
        self.assertEqual(bought.strategy_stop_loss, 8.5)
        self.assertEqual(sold.strategy_status, "FLAT")
        self.assertEqual(raw["positions"]["000001.SZ"]["strategy_stop_loss"], 8.5)
        self.assertEqual(raw["positions"]["000001.SZ"]["strategy_exit_date"], "2026-01-09")


class IntegrationTests(unittest.TestCase):
    def test_runner_allows_modules_to_be_swapped(self):
        tmp = case_dir("integration")
        candidate = StockCandidate("000001.SZ")
        start = datetime(2026, 1, 2, 10, 0)
        sender = FakeEmailSender()
        runner = Runner(
            selector=FakeSelector([candidate]),
            market_data=FakeMarketData(
                bars=make_atr_bars(count=56, high=10.0, low=9.0, close=9.5),
                quotes=[
                    Quote("000001.SZ", start, 10.5),
                    Quote("000001.SZ", start + timedelta(minutes=5), 10.6),
                ],
            ),
            strategy=TurtleStrategyEngine(55, 20, 20, 0.01, confirm_minutes=0),
            trade_gateway=AlertTradeGateway(
                notifier=EmailNotifier(sender),
                signal_store=SignalStore(str(tmp / "signals.json")),
                orders_dir=str(tmp / "orders"),
            ),
            positions={},
        )

        first = runner.run_once(as_of=date(2026, 1, 2), at=start)
        second = runner.run_once(as_of=date(2026, 1, 2), at=start + timedelta(minutes=5))

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(sender.messages), 1)

    def test_runner_writes_portfolio_state_after_buy_signal(self):
        tmp = case_dir("integration_portfolio_buy")
        portfolio_path = tmp / "portfolio.json"
        portfolio_path.write_text(
            json.dumps({"cash": 100000, "positions": {}}),
            encoding="utf-8",
        )
        repository = PortfolioRepository(str(portfolio_path))
        candidate = StockCandidate("000001.SZ")
        start = datetime(2026, 1, 2, 10, 0)
        runner = Runner(
            selector=FakeSelector([candidate]),
            market_data=FakeMarketData(
                bars=make_atr_bars(count=56, high=10.0, low=9.0, close=9.5),
                quotes=[Quote("000001.SZ", start, 10.5)],
            ),
            strategy=TurtleStrategyEngine(55, 20, 20, 0.01, confirm_minutes=0),
            trade_gateway=AlertTradeGateway(
                notifier=EmailNotifier(FakeEmailSender()),
                signal_store=SignalStore(str(tmp / "signals.json")),
                orders_dir=str(tmp / "orders"),
            ),
            positions=repository.load_positions(),
            portfolio_repository=repository,
        )

        emitted = runner.run_once(as_of=date(2026, 1, 2), at=start)
        positions = repository.load_positions()

        self.assertEqual(emitted, 1)
        self.assertEqual(positions["000001.SZ"].strategy_status, "LONG")
        self.assertEqual(positions["000001.SZ"].strategy_entry_price, 10.5)
        self.assertEqual(positions["000001.SZ"].strategy_stop_loss, 8.5)

    def test_runner_scans_existing_strategy_positions_for_sell_signals(self):
        tmp = case_dir("integration_portfolio_sell_scan")
        start = datetime(2026, 1, 2, 10, 0)
        sender = FakeEmailSender()
        runner = Runner(
            selector=FakeSelector([]),
            market_data=FakeMarketData(
                bars=make_atr_bars(count=56, high=10.0, low=8.0, close=9.5),
                quotes=[Quote("000001.SZ", start, 7.5)],
            ),
            strategy=TurtleStrategyEngine(55, 20, 20, 0.01, confirm_minutes=0),
            trade_gateway=AlertTradeGateway(
                notifier=EmailNotifier(sender),
                signal_store=SignalStore(str(tmp / "signals.json")),
                orders_dir=str(tmp / "orders"),
            ),
            positions={"000001.SZ": Position("000001.SZ", strategy_status="LONG")},
        )

        emitted = runner.run_once(as_of=date(2026, 1, 2), at=start)

        self.assertEqual(emitted, 1)
        self.assertEqual(len(sender.messages), 1)
        self.assertIn("SELL 000001.SZ", sender.messages[0][0])

    def test_runner_deduplicates_selected_existing_positions(self):
        tmp = case_dir("integration_portfolio_scan_dedupe")
        candidate = StockCandidate("000001.SZ")
        start = datetime(2026, 1, 2, 10, 0)

        class CountingQuoteMarketData(CountingMarketData):
            def get_quote(self, symbol: str, at: datetime | None = None):
                return Quote(symbol, at or start, 8.5)

        market_data = CountingQuoteMarketData({"000001.SZ": make_atr_bars(count=56, high=10.0, low=8.0, close=9.5)})
        runner = Runner(
            selector=FakeSelector([candidate]),
            market_data=market_data,
            strategy=TurtleStrategyEngine(55, 20, 20, 0.01, confirm_minutes=0),
            trade_gateway=AlertTradeGateway(
                notifier=EmailNotifier(FakeEmailSender()),
                signal_store=SignalStore(str(tmp / "signals.json")),
                orders_dir=str(tmp / "orders"),
            ),
            positions={"000001.SZ": Position("000001.SZ", strategy_status="LONG")},
        )

        emitted = runner.run_once(as_of=date(2026, 1, 2), at=start)

        self.assertEqual(emitted, 0)
        self.assertEqual(market_data.calls, ["000001.SZ"])

    def test_runner_dry_run_does_not_write_portfolio_state(self):
        tmp = case_dir("integration_portfolio_dry_run")
        portfolio_path = tmp / "portfolio.json"
        portfolio_path.write_text(
            json.dumps({"cash": 100000, "positions": {}}),
            encoding="utf-8",
        )
        repository = PortfolioRepository(str(portfolio_path))
        candidate = StockCandidate("000001.SZ")
        start = datetime(2026, 1, 2, 10, 0)
        runner = Runner(
            selector=FakeSelector([candidate]),
            market_data=FakeMarketData(
                bars=make_atr_bars(count=56, high=10.0, low=9.0, close=9.5),
                quotes=[Quote("000001.SZ", start, 10.5)],
            ),
            strategy=TurtleStrategyEngine(55, 20, 20, 0.01, confirm_minutes=0),
            trade_gateway=AlertTradeGateway(
                notifier=EmailNotifier(FakeEmailSender()),
                signal_store=SignalStore(str(tmp / "signals.json")),
                orders_dir=str(tmp / "orders"),
                dry_run=True,
            ),
            positions=repository.load_positions(),
            portfolio_repository=repository,
        )

        emitted = runner.run_once(as_of=date(2026, 1, 2), at=start)
        positions = repository.load_positions()

        self.assertEqual(emitted, 1)
        self.assertNotIn("000001.SZ", positions)


if __name__ == "__main__":
    unittest.main()
