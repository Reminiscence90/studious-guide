"""Where review-chart candles come from.

=============  ==========================================  =========================
Source         Markets                                     Access
=============  ==========================================  =========================
OANDA          forex, spot metals (XAU/XAG), oil CFDs      free practice-account token
Twelve Data    forex, spot metals, crypto, US stocks       free API key (800 calls/day)
Binance        crypto (USDT pairs)                         public, no key
Yahoo Finance  US stocks, futures (and fallback for all)   public, no key
=============  ==========================================  =========================

``auto`` picks, per instrument: crypto → Binance; forex and spot commodities → OANDA if
a token is set, else Twelve Data if a key is set, else Yahoo; everything else → Yahoo.
All sources return bars with naive UTC open times (see ``core.market_data``).
TradingView has no public data API; its "Export chart data" CSV can be uploaded instead.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from core import http
from core.instruments import STABLE_QUOTES, AssetClass, InstrumentSpec, default_chart_symbol
from core.market_data import (
    BAR_COLUMNS,
    INTERVALS,
    MarketDataError,
    fetch_yahoo,
    resample,
)
from core.models import AppSetting

GetJson = Callable[..., Any]
SOURCE_LABELS = {
    "auto": "Auto",
    "oanda": "OANDA",
    "twelvedata": "Twelve Data",
    "binance": "Binance",
    "yahoo": "Yahoo Finance",
}

# --------------------------------------------------------------------------- settings

SETTING_ENV = {
    "oanda_token": "OANDA_API_TOKEN",
    "oanda_environment": "OANDA_ENVIRONMENT",
    "twelvedata_api_key": "TWELVEDATA_API_KEY",
}


def get_setting(session: Session, key: str, default: str = "") -> str:
    """A setting from the environment (if set) or the database."""
    env = SETTING_ENV.get(key)
    if env and os.environ.get(env):
        return os.environ[env]
    row = session.get(AppSetting, key)
    return row.value if row and row.value else default


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value.strip()))
    else:
        row.value = value.strip()
    session.flush()


def credentials(session: Session) -> dict[str, str]:
    return {
        "oanda_token": get_setting(session, "oanda_token"),
        "oanda_environment": get_setting(session, "oanda_environment", "practice"),
        "twelvedata_api_key": get_setting(session, "twelvedata_api_key"),
    }


def settings_from_db(session: Session) -> dict[str, str]:
    """Only what is stored in the database (environment variables excluded)."""
    return {r.key: r.value for r in session.scalars(select(AppSetting))}


# --------------------------------------------------------------------------- helpers

_SPOT_FX_LIKE = {"XAUUSD", "XAGUSD", "XPTUSD", "USOIL", "UKOIL"}


def _crypto_base(symbol: str) -> str:
    return next((symbol.removesuffix(q) for q in STABLE_QUOTES if symbol.endswith(q)), symbol)


def _rfc3339(t: datetime) -> str:
    return t.replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunks(start: datetime, end: datetime, interval: str, max_bars: int):
    step = timedelta(seconds=INTERVALS[interval] * max_bars)
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(end, chunk_start + step)
        yield chunk_start, chunk_end
        chunk_start = chunk_end


def _frame(rows: list[tuple]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = pd.DataFrame(rows, columns=BAR_COLUMNS)
    df["time"] = pd.to_datetime(df["time"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).drop_duplicates("time")
    return df.sort_values("time").reset_index(drop=True)


# --------------------------------------------------------------------------- OANDA

OANDA_HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}
OANDA_GRANULARITY = {"1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30", "1h": "H1"}
_OANDA_SPECIAL = {"USOIL": "WTICO_USD", "UKOIL": "BCO_USD"}


def oanda_ticker(spec: InstrumentSpec) -> str:
    """EURUSD → EUR_USD, XAUUSD → XAU_USD, USOIL → WTICO_USD."""
    sym = spec.symbol
    if sym in _OANDA_SPECIAL:
        return _OANDA_SPECIAL[sym]
    return f"{sym[:3]}_{sym[3:]}" if len(sym) == 6 else sym


def parse_oanda_candles(payload: dict) -> pd.DataFrame:
    """Completed bid candles from ``/v3/instruments/{i}/candles``."""
    rows = []
    for c in payload.get("candles", []):
        price = c.get("bid") or c.get("mid")
        if not price or not c.get("complete", True):
            continue
        t = pd.Timestamp(c["time"]).tz_convert("UTC").tz_localize(None)
        rows.append((t, price["o"], price["h"], price["l"], price["c"], c.get("volume")))
    return _frame(rows)


def fetch_oanda(
    ticker: str,
    interval: str,
    start: datetime,
    end: datetime,
    *,
    token: str,
    environment: str = "practice",
    get_json: GetJson = http.get_json,
) -> pd.DataFrame:
    """Bid candles from OANDA's v20 API (4h and 1d are built from hourly candles, so they
    line up with UTC like the other sources)."""
    if not token:
        raise MarketDataError(
            "OANDA needs an API token — add it under Settings → Market data (free with an "
            "OANDA practice account)."
        )
    if interval in ("4h", "1d"):
        return resample(
            fetch_oanda(ticker, "1h", start, end, token=token, environment=environment,
                        get_json=get_json),
            interval,
        )  # fmt: skip
    host = OANDA_HOSTS.get(environment, OANDA_HOSTS["practice"])
    frames = []
    for a, b in _chunks(start, end, interval, 4900):
        params = urllib.parse.urlencode(
            {
                "price": "B",
                "granularity": OANDA_GRANULARITY[interval],
                "from": _rfc3339(a),
                "to": _rfc3339(b),
            }
        )
        url = f"{host}/v3/instruments/{urllib.parse.quote(ticker)}/candles?{params}"
        try:
            payload = get_json(url, headers={"Authorization": f"Bearer {token}"})
        except http.HttpError as exc:
            hint = " — check the token and practice/live setting" if exc.status == 401 else ""
            raise MarketDataError(f"OANDA: {exc}{hint}") from exc
        frames.append(parse_oanda_candles(payload))
    return _frame([tuple(r) for f in frames for r in f.itertuples(index=False)])


# --------------------------------------------------------------------------- Twelve Data

TWELVEDATA_INTERVAL = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1day",
}


def twelvedata_ticker(spec: InstrumentSpec) -> str:
    """EURUSD → EUR/USD, XAUUSD → XAU/USD, BTCUSD → BTC/USD, TSLA → TSLA."""
    sym = spec.symbol
    if spec.asset_class is AssetClass.CRYPTO:
        return f"{_crypto_base(sym)}/USD"
    if spec.asset_class is AssetClass.FOREX or sym in _SPOT_FX_LIKE:
        if sym == "USOIL":
            return "WTI/USD"
        if sym == "UKOIL":
            return "XBR/USD"
        return f"{sym[:3]}/{sym[3:]}" if len(sym) == 6 else sym
    return sym


def parse_twelvedata(payload: dict) -> pd.DataFrame:
    if payload.get("status") == "error":
        raise MarketDataError(f"Twelve Data: {payload.get('message', 'request failed')}")
    rows = [
        (v["datetime"], v["open"], v["high"], v["low"], v["close"], v.get("volume"))
        for v in payload.get("values", [])
    ]
    return _frame(rows)


def fetch_twelvedata(
    ticker: str,
    interval: str,
    start: datetime,
    end: datetime,
    *,
    api_key: str,
    get_json: GetJson = http.get_json,
) -> pd.DataFrame:
    if not api_key:
        raise MarketDataError(
            "Twelve Data needs an API key — add it under Settings → Market data (free at "
            "twelvedata.com)."
        )
    frames = []
    for a, b in _chunks(start, end, interval, 4900):
        params = urllib.parse.urlencode(
            {
                "symbol": ticker,
                "interval": TWELVEDATA_INTERVAL[interval],
                "start_date": a.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": b.strftime("%Y-%m-%d %H:%M:%S"),
                "timezone": "UTC",
                "order": "ASC",
                "outputsize": 5000,
                "apikey": api_key,
            }
        )
        try:
            payload = get_json(f"https://api.twelvedata.com/time_series?{params}")
        except http.HttpError as exc:
            raise MarketDataError(f"Twelve Data: {exc}") from exc
        if payload.get("status") == "error" and "No data" in str(payload.get("message", "")):
            continue  # e.g. a weekend chunk
        frames.append(parse_twelvedata(payload))
    return _frame([tuple(r) for f in frames for r in f.itertuples(index=False)])


# --------------------------------------------------------------------------- Binance


def binance_ticker(spec: InstrumentSpec) -> str:
    """BTCUSD → BTCUSDT (Binance quotes in Tether, which tracks the dollar)."""
    return f"{_crypto_base(spec.symbol)}USDT"


def parse_binance(payload: list) -> pd.DataFrame:
    rows = [(pd.Timestamp(k[0], unit="ms"), k[1], k[2], k[3], k[4], k[5]) for k in payload or []]
    return _frame(rows)


def fetch_binance(
    ticker: str,
    interval: str,
    start: datetime,
    end: datetime,
    *,
    get_json: GetJson = http.get_json,
) -> pd.DataFrame:
    """Klines from Binance's public market-data endpoint (no key)."""
    frames = []
    for a, b in _chunks(start, end, interval, 1000):
        params = urllib.parse.urlencode(
            {
                "symbol": ticker,
                "interval": interval,
                "startTime": int(a.replace(tzinfo=UTC).timestamp() * 1000),
                "endTime": int(b.replace(tzinfo=UTC).timestamp() * 1000) - 1,
                "limit": 1000,
            }
        )
        try:
            payload = get_json(f"https://data-api.binance.vision/api/v3/klines?{params}")
        except http.HttpError as exc:
            raise MarketDataError(f"Binance: {exc}") from exc
        frames.append(parse_binance(payload))
    return _frame([tuple(r) for f in frames for r in f.itertuples(index=False)])


# --------------------------------------------------------------------------- choice


@dataclass(frozen=True)
class ChartSource:
    """A resolved data source for one instrument."""

    source: str  # "yahoo" | "oanda" | "twelvedata" | "binance"
    ticker: str
    fetch: Callable[[str, str, datetime, datetime], pd.DataFrame]

    @property
    def label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def cache_key(self) -> str:
        return f"{self.source}:{self.ticker}"

    @property
    def history_limits(self) -> bool:
        """Only Yahoo limits how far back intraday candles go."""
        return self.source == "yahoo"


DEFAULT_TICKERS: dict[str, Callable[[InstrumentSpec], str]] = {
    "yahoo": default_chart_symbol,
    "oanda": oanda_ticker,
    "twelvedata": twelvedata_ticker,
    "binance": binance_ticker,
}


def auto_source(spec: InstrumentSpec, creds: dict[str, str]) -> str:
    if spec.asset_class is AssetClass.CRYPTO:
        return "binance"
    if spec.asset_class is AssetClass.FOREX or spec.symbol in _SPOT_FX_LIKE:
        if creds.get("oanda_token"):
            return "oanda"
        if creds.get("twelvedata_api_key"):
            return "twelvedata"
    return "yahoo"


def chart_source(
    spec: InstrumentSpec, creds: dict[str, str], get_json: GetJson = http.get_json
) -> ChartSource:
    """Pick the data source and ticker for an instrument (``spec.data_source`` wins)."""
    source = spec.data_source if spec.data_source in DEFAULT_TICKERS else auto_source(spec, creds)
    ticker = spec.chart_symbol or DEFAULT_TICKERS[source](spec)
    if source == "oanda":

        def fetch(t: str, i: str, a: datetime, b: datetime) -> pd.DataFrame:
            return fetch_oanda(
                t, i, a, b, token=creds.get("oanda_token", ""),
                environment=creds.get("oanda_environment", "practice"), get_json=get_json,
            )  # fmt: skip
    elif source == "twelvedata":

        def fetch(t: str, i: str, a: datetime, b: datetime) -> pd.DataFrame:
            return fetch_twelvedata(
                t, i, a, b, api_key=creds.get("twelvedata_api_key", ""), get_json=get_json
            )
    elif source == "binance":

        def fetch(t: str, i: str, a: datetime, b: datetime) -> pd.DataFrame:
            return fetch_binance(t, i, a, b, get_json=get_json)
    else:

        def fetch(t: str, i: str, a: datetime, b: datetime) -> pd.DataFrame:
            return fetch_yahoo(t, i, a, b, get_json=get_json)

    fetch.source = source  # type: ignore[attr-defined]
    return ChartSource(source, ticker, fetch)
