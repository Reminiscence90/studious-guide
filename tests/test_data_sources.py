from __future__ import annotations

import json
import ssl
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy.orm import Session

from core import data_sources as ds
from core import http
from core.instruments import AssetClass, InstrumentSpec, default_specs
from core.market_data import MarketDataError, get_bars

SPECS = default_specs()
T0 = datetime(2026, 9, 16, 17, 20)


def oanda_payload(start: datetime, n: int, step: int = 60) -> dict:
    candles = []
    for i in range(n):
        t = start + timedelta(seconds=i * step)
        p = 4350 + i
        candles.append(
            {
                "complete": True,
                "volume": 10 + i,
                "time": t.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
                "bid": {"o": f"{p}", "h": f"{p + 1}", "l": f"{p - 1}", "c": f"{p + 0.5}"},
            }
        )
    return {"instrument": "XAU_USD", "granularity": "M1", "candles": candles}


# --------------------------------------------------------------------------- tickers


@pytest.mark.parametrize(
    ("symbol", "oanda", "twelve"),
    [
        ("EURUSD", "EUR_USD", "EUR/USD"),
        ("USDJPY", "USD_JPY", "USD/JPY"),
        ("XAUUSD", "XAU_USD", "XAU/USD"),
        ("XAGUSD", "XAG_USD", "XAG/USD"),
        ("USOIL", "WTICO_USD", "WTI/USD"),
        ("BTCUSD", "BTC_USD", "BTC/USD"),
        ("TSLA", "TSLA", "TSLA"),
    ],
)
def test_default_tickers(symbol: str, oanda: str, twelve: str) -> None:
    spec = SPECS[symbol]
    assert ds.oanda_ticker(spec) == oanda
    assert ds.twelvedata_ticker(spec) == twelve


def test_binance_ticker() -> None:
    assert ds.binance_ticker(SPECS["ETHUSD"]) == "ETHUSDT"


# --------------------------------------------------------------------------- OANDA


def test_fetch_oanda_bid_candles() -> None:
    calls: list[tuple[str, dict]] = []

    def fake(url: str, headers: dict | None = None) -> dict:
        calls.append((url, headers or {}))
        payload = oanda_payload(T0, 3)
        payload["candles"].append({**payload["candles"][-1], "complete": False})  # forming bar
        return payload

    bars = ds.fetch_oanda("XAU_USD", "1m", T0, T0 + timedelta(minutes=3), token="tok",
                          get_json=fake)  # fmt: skip
    assert len(bars) == 3  # the incomplete candle is dropped
    assert bars["time"].iloc[0] == T0
    assert bars["close"].tolist() == [4350.5, 4351.5, 4352.5]
    url, headers = calls[0]
    query = parse_qs(urlparse(url).query)
    assert url.startswith("https://api-fxpractice.oanda.com/v3/instruments/XAU_USD/candles")
    assert query["price"] == ["B"] and query["granularity"] == ["M1"]
    assert query["from"] == ["2026-09-16T17:20:00Z"]
    assert headers["Authorization"] == "Bearer tok"


def test_fetch_oanda_live_host_chunks_and_4h() -> None:
    urls: list[str] = []

    def fake(url: str, headers: dict | None = None) -> dict:
        urls.append(url)
        return oanda_payload(T0, 8, step=3600)

    bars = ds.fetch_oanda("EUR_USD", "4h", T0, T0 + timedelta(hours=8), token="t",
                          environment="live", get_json=fake)  # fmt: skip
    assert urls[0].startswith("https://api-fxtrade.oanda.com")
    assert "granularity=H1" in urls[0]
    assert len(bars) == 3  # 17:20.. hourly bars fall into the 16:00, 20:00 and 00:00 4h bars
    urls.clear()
    ds.fetch_oanda("EUR_USD", "1m", T0, T0 + timedelta(days=7), token="t", get_json=fake)
    assert len(urls) == 3  # at most 4900 candles per request


def test_oanda_needs_token_and_explains_401() -> None:
    with pytest.raises(MarketDataError, match="needs an API token"):
        ds.fetch_oanda("EUR_USD", "1m", T0, T0 + timedelta(minutes=5), token="")

    def denied(url: str, headers: dict | None = None) -> dict:
        raise http.HttpError("HTTP 401: Insufficient authorization", 401)

    with pytest.raises(MarketDataError, match="check the token"):
        ds.fetch_oanda("EUR_USD", "1m", T0, T0 + timedelta(minutes=5), token="x",
                       get_json=denied)  # fmt: skip


# --------------------------------------------------------------------------- Twelve Data


def test_fetch_twelvedata() -> None:
    urls: list[str] = []

    def fake(url: str) -> dict:
        urls.append(url)
        return {
            "meta": {"symbol": "XAU/USD", "interval": "1min"},
            "values": [
                {"datetime": "2026-09-16 17:20:00", "open": "4350.1", "high": "4351",
                 "low": "4349", "close": "4350.5"},
                {"datetime": "2026-09-16 17:21:00", "open": "4350.5", "high": "4352",
                 "low": "4350", "close": "4351.2"},
            ],
            "status": "ok",
        }  # fmt: skip

    bars = ds.fetch_twelvedata("XAU/USD", "1m", T0, T0 + timedelta(minutes=2), api_key="k",
                               get_json=fake)  # fmt: skip
    assert bars["close"].tolist() == [4350.5, 4351.2]
    query = parse_qs(urlparse(urls[0]).query)
    assert query["symbol"] == ["XAU/USD"] and query["interval"] == ["1min"]
    assert query["timezone"] == ["UTC"] and query["apikey"] == ["k"]


def test_twelvedata_errors() -> None:
    with pytest.raises(MarketDataError, match="needs an API key"):
        ds.fetch_twelvedata("EUR/USD", "1m", T0, T0 + timedelta(minutes=1), api_key="")

    def limited(url: str) -> dict:
        return {"code": 429, "message": "You have run out of API credits", "status": "error"}

    with pytest.raises(MarketDataError, match="run out of API credits"):
        ds.fetch_twelvedata("EUR/USD", "1m", T0, T0 + timedelta(minutes=1), api_key="k",
                            get_json=limited)  # fmt: skip

    def weekend(url: str) -> dict:
        return {"code": 400, "message": "No data is available on the specified dates.",
                "status": "error"}  # fmt: skip

    assert ds.fetch_twelvedata("EUR/USD", "1m", T0, T0 + timedelta(minutes=1), api_key="k",
                               get_json=weekend).empty  # fmt: skip


# --------------------------------------------------------------------------- Binance


def test_fetch_binance() -> None:
    urls: list[str] = []
    ms = int((T0 - datetime(1970, 1, 1)).total_seconds() * 1000)  # T0 as UTC epoch ms

    def fake(url: str) -> list:
        urls.append(url)
        return [[ms, "112000.1", "112100", "111900", "112050.5", "12.5", ms + 59999]]

    bars = ds.fetch_binance("BTCUSDT", "1m", T0, T0 + timedelta(minutes=1), get_json=fake)
    assert bars["time"].iloc[0] == T0
    assert bars["close"].iloc[0] == 112050.5
    assert bars["volume"].iloc[0] == 12.5
    query = parse_qs(urlparse(urls[0]).query)
    assert urls[0].startswith("https://data-api.binance.vision/api/v3/klines")
    assert query["symbol"] == ["BTCUSDT"] and query["startTime"] == [str(ms)]


# --------------------------------------------------------------------------- choice


def test_auto_source() -> None:
    none: dict[str, str] = {}
    assert ds.auto_source(SPECS["BTCUSD"], none) == "binance"
    assert ds.auto_source(SPECS["XAUUSD"], none) == "yahoo"
    assert ds.auto_source(SPECS["XAUUSD"], {"twelvedata_api_key": "k"}) == "twelvedata"
    assert ds.auto_source(SPECS["EURUSD"], {"oanda_token": "t", "twelvedata_api_key": "k"}) == (
        "oanda"
    )
    assert ds.auto_source(SPECS["TSLA"], {"oanda_token": "t"}) == "yahoo"
    assert ds.auto_source(SPECS["ES"], {"twelvedata_api_key": "k"}) == "yahoo"


def test_chart_source_overrides_and_keys() -> None:
    src = ds.chart_source(SPECS["XAUUSD"], {"oanda_token": "t"})
    assert (src.source, src.ticker, src.cache_key) == ("oanda", "XAU_USD", "oanda:XAU_USD")
    assert not src.history_limits
    yahoo = ds.chart_source(SPECS["XAUUSD"], {})
    assert (yahoo.ticker, yahoo.history_limits) == ("GC=F", True)
    custom = InstrumentSpec("XAUUSD", AssetClass.COMMODITY, 100, data_source="twelvedata",
                            chart_symbol="XAU/USD")  # fmt: skip
    src = ds.chart_source(custom, {"oanda_token": "t"})
    assert (src.source, src.ticker, src.label) == ("twelvedata", "XAU/USD", "Twelve Data")


def test_chart_source_fetch_is_cached_under_source_key(session: Session) -> None:
    def fake(url: str, headers: dict | None = None) -> dict:
        return oanda_payload(T0, 30)

    src = ds.chart_source(SPECS["XAUUSD"], {"oanda_token": "t"}, get_json=fake)
    bars, source = get_bars(session, src.cache_key, "1m", T0, T0 + timedelta(minutes=29),
                            fetch=src.fetch, ticker=src.ticker)  # fmt: skip
    assert (len(bars), source) == (30, "oanda")


# --------------------------------------------------------------------------- settings


def test_settings_store_and_env_override(session: Session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert ds.get_setting(session, "twelvedata_api_key") == ""
    ds.set_setting(session, "twelvedata_api_key", "  abc  ")
    assert ds.get_setting(session, "twelvedata_api_key") == "abc"
    assert ds.credentials(session)["oanda_environment"] == "practice"
    monkeypatch.setenv("TWELVEDATA_API_KEY", "from-env")
    assert ds.get_setting(session, "twelvedata_api_key") == "from-env"
    assert ds.settings_from_db(session)["twelvedata_api_key"] == "abc"


# --------------------------------------------------------------------------- http


def test_ssl_context_uses_a_verifying_context() -> None:
    ctx = http.ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_get_json_reports_http_errors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for var in ("NO_PROXY", "no_proxy"):  # talk to the local server directly
        monkeypatch.setenv(var, "127.0.0.1,localhost")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"errorMessage": "Insufficient authorization"}).encode()
            self.send_response(401 if "deny" in self.path else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body if "deny" in self.path else b'{"ok": true}')

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert http.get_json(f"{base}/ok") == {"ok": True}
        with pytest.raises(http.HttpError, match="401: Insufficient authorization") as err:
            http.get_json(f"{base}/deny")
        assert err.value.status == 401
    finally:
        server.shutdown()
