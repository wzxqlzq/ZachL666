import csv
import shutil
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TMP_ROOT = Path(__file__).resolve().parents[1] / "test_tmp"
TMP_ROOT.mkdir(exist_ok=True)

from market_data import AkshareMarketDataProvider, CsvMarketDataProvider, EastmoneyMarketDataProvider, FallbackMarketDataProvider
from models import Bar, Position, Quote, Signal, StockCandidate
from notifier import EmailNotifier, EmailSender
from offline_data import OfflineDataStore, OfflineDataSync, OfflineMarketDataProvider
from runner import Runner
from signal_store import SignalStore
from stock_selector import CsvStockSelector, RuleBasedStockSelector
from strategy import TurtleStrategyEngine
from trade_gateway import AlertTradeGateway
from universe_provider import AkshareStockUniverseProvider, FallbackStockUniverseProvider


class FakeEmailSender(EmailSender):
    def __init__(self):
        self.messages = []

    def send(self, subject: str, body: str) -> None:
        self.messages.append((subject, body))


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
            MapMarketData({"000001.SZ": make_selector_bars()}),
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
            "000001.SZ": make_selector_bars("000001.SZ"),
            "000002.SZ": make_selector_bars("000002.SZ"),
            "430001.BJ": make_selector_bars("430001.BJ"),
            "000003.SZ": make_selector_bars("000003.SZ"),
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

    def test_rule_selector_can_use_offline_provider(self):
        tmp = case_dir("offline_selection")
        store = OfflineDataStore(str(tmp / "offline"))
        store.save_universe(FakeUniverseProvider().load_universe())
        store.save_bars("000001.SZ", make_selector_bars("000001.SZ"))

        selector = RuleBasedStockSelector(
            universe_csv_path=str(store.universe_path),
            market_data=OfflineMarketDataProvider(store),
        )

        result = selector.select(date(2026, 5, 1))

        self.assertEqual([item.symbol for item in result], ["000001.SZ"])

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

    def test_fallback_universe_provider_uses_fallback_on_failure(self):
        fallback = FakeUniverseProvider()
        provider = FallbackStockUniverseProvider(FailingProvider(), fallback)

        rows = provider.load_universe()

        self.assertEqual(rows[0]["symbol"], "000001.SZ")


class StrategyTests(unittest.TestCase):
    def test_buy_breakout_requires_five_minute_confirmation(self):
        strategy = TurtleStrategyEngine(20, 10, 20, 0.01, confirm_minutes=5)
        candidate = StockCandidate("000001.SZ")
        bars = make_bars()
        start = datetime(2026, 1, 2, 10, 0)

        first = strategy.on_quote(candidate, bars, Quote("000001.SZ", start, 10.5), Position("000001.SZ"))
        second = strategy.on_quote(
            candidate,
            bars,
            Quote("000001.SZ", start + timedelta(minutes=4), 10.6),
            Position("000001.SZ"),
        )
        third = strategy.on_quote(
            candidate,
            bars,
            Quote("000001.SZ", start + timedelta(minutes=5), 10.7),
            Position("000001.SZ"),
        )
        duplicate = strategy.on_quote(
            candidate,
            bars,
            Quote("000001.SZ", start + timedelta(minutes=10), 10.8),
            Position("000001.SZ"),
        )

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(third[0].action, "BUY")
        self.assertEqual(duplicate, [])

    def test_pending_breakout_is_cancelled_when_price_reverts(self):
        strategy = TurtleStrategyEngine(20, 10, 20, 0.01, confirm_minutes=5)
        candidate = StockCandidate("000001.SZ")
        bars = make_bars()
        start = datetime(2026, 1, 2, 10, 0)

        strategy.on_quote(candidate, bars, Quote("000001.SZ", start, 10.5), Position("000001.SZ"))
        reverted = strategy.on_quote(
            candidate,
            bars,
            Quote("000001.SZ", start + timedelta(minutes=5), 9.9),
            Position("000001.SZ"),
        )

        self.assertEqual(reverted, [])

    def test_sell_signal_for_existing_position(self):
        strategy = TurtleStrategyEngine(20, 10, 20, 0.01, confirm_minutes=5)
        candidate = StockCandidate("000001.SZ")
        bars = make_bars(low=8.0)
        start = datetime(2026, 1, 2, 10, 0)
        position = Position("000001.SZ", shares=100)

        strategy.on_quote(candidate, bars, Quote("000001.SZ", start, 7.5), position)
        signals = strategy.on_quote(candidate, bars, Quote("000001.SZ", start + timedelta(minutes=5), 7.4), position)

        self.assertEqual(signals[0].action, "SELL")


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


class IntegrationTests(unittest.TestCase):
    def test_runner_allows_modules_to_be_swapped(self):
        tmp = case_dir("integration")
        candidate = StockCandidate("000001.SZ")
        start = datetime(2026, 1, 2, 10, 0)
        sender = FakeEmailSender()
        runner = Runner(
            selector=FakeSelector([candidate]),
            market_data=FakeMarketData(
                bars=make_bars(),
                quotes=[
                    Quote("000001.SZ", start, 10.5),
                    Quote("000001.SZ", start + timedelta(minutes=5), 10.6),
                ],
            ),
            strategy=TurtleStrategyEngine(20, 10, 20, 0.01, confirm_minutes=5),
            trade_gateway=AlertTradeGateway(
                notifier=EmailNotifier(sender),
                signal_store=SignalStore(str(tmp / "signals.json")),
                orders_dir=str(tmp / "orders"),
            ),
            positions={},
        )

        first = runner.run_once(as_of=date(2026, 1, 2), at=start)
        second = runner.run_once(as_of=date(2026, 1, 2), at=start + timedelta(minutes=5))

        self.assertEqual(first, 0)
        self.assertEqual(second, 1)
        self.assertEqual(len(sender.messages), 1)


if __name__ == "__main__":
    unittest.main()
