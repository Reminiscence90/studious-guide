"""Instrument specs: asset class, contract multiplier and quote currency.

P&L is ``price move × quantity × point value``, where the point value is the USD
value of a 1.0 price move for one unit of quantity:

* **US stocks / crypto**: quantity in shares or coins, multiplier 1.
* **Futures**: quantity in contracts, multiplier = contract point value
  (ES $50, NQ $20, CL $1,000, GC $100, ...).
* **Forex**: quantity in standard lots, multiplier 100,000 units per lot. For
  USD-quoted pairs (EURUSD) the P&L is already in USD. For USD-base pairs (USDJPY,
  USDCAD, USDCHF) the P&L is in the quote currency and is converted to USD by
  dividing by the exit price.
* **Spot commodities / CFDs** (XAUUSD, USOIL, ...): quantity in lots, multiplier =
  units per lot (gold 100 oz, silver 5,000 oz, oil 1,000 barrels).

The catalog below is the default; every value can be changed in Settings.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable
from dataclasses import dataclass

ACCOUNT_CURRENCY = "USD"


class AssetClass(enum.StrEnum):
    STOCK = "stock"
    FUTURE = "future"
    FOREX = "forex"
    COMMODITY = "commodity"
    CRYPTO = "crypto"

    @property
    def label(self) -> str:
        return {
            AssetClass.STOCK: "US stocks",
            AssetClass.FUTURE: "Futures",
            AssetClass.FOREX: "Forex",
            AssetClass.COMMODITY: "Commodities",
            AssetClass.CRYPTO: "Crypto",
        }[self]


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    asset_class: AssetClass
    multiplier: float = 1.0
    quote_currency: str = "USD"
    name: str = ""


_F, _C, _X, _K, _S = (
    AssetClass.FUTURE,
    AssetClass.COMMODITY,
    AssetClass.FOREX,
    AssetClass.CRYPTO,
    AssetClass.STOCK,
)
FOREX_LOT = 100_000.0

DEFAULT_INSTRUMENTS: list[InstrumentSpec] = [
    # US stocks (anything unknown is treated as a US stock, these are just examples)
    InstrumentSpec("TSLA", _S, 1, "USD", "Tesla Inc."),
    InstrumentSpec("NVDA", _S, 1, "USD", "NVIDIA Corp."),
    InstrumentSpec("AAPL", _S, 1, "USD", "Apple Inc."),
    # Equity index / rates / currency futures (CME)
    InstrumentSpec("ES", _F, 50, "USD", "E-mini S&P 500"),
    InstrumentSpec("MES", _F, 5, "USD", "Micro E-mini S&P 500"),
    InstrumentSpec("NQ", _F, 20, "USD", "E-mini Nasdaq-100"),
    InstrumentSpec("MNQ", _F, 2, "USD", "Micro E-mini Nasdaq-100"),
    InstrumentSpec("YM", _F, 5, "USD", "E-mini Dow"),
    InstrumentSpec("MYM", _F, 0.5, "USD", "Micro E-mini Dow"),
    InstrumentSpec("RTY", _F, 50, "USD", "E-mini Russell 2000"),
    InstrumentSpec("M2K", _F, 5, "USD", "Micro E-mini Russell 2000"),
    InstrumentSpec("ZN", _F, 1000, "USD", "10-Year T-Note"),
    InstrumentSpec("ZB", _F, 1000, "USD", "30-Year T-Bond"),
    InstrumentSpec("6E", _F, 125_000, "USD", "Euro FX"),
    InstrumentSpec("6B", _F, 62_500, "USD", "British Pound"),
    # Commodity futures
    InstrumentSpec("CL", _C, 1000, "USD", "Crude Oil (WTI) futures"),
    InstrumentSpec("MCL", _C, 100, "USD", "Micro Crude Oil futures"),
    InstrumentSpec("NG", _C, 10_000, "USD", "Natural Gas futures"),
    InstrumentSpec("GC", _C, 100, "USD", "Gold futures"),
    InstrumentSpec("MGC", _C, 10, "USD", "Micro Gold futures"),
    InstrumentSpec("SI", _C, 5000, "USD", "Silver futures"),
    InstrumentSpec("HG", _C, 25_000, "USD", "Copper futures"),
    # Spot commodities / CFDs (quantity in lots)
    InstrumentSpec("XAUUSD", _C, 100, "USD", "Gold spot (100 oz lot)"),
    InstrumentSpec("XAGUSD", _C, 5000, "USD", "Silver spot (5,000 oz lot)"),
    InstrumentSpec("USOIL", _C, 1000, "USD", "WTI crude CFD (1,000 bbl lot)"),
    InstrumentSpec("UKOIL", _C, 1000, "USD", "Brent crude CFD (1,000 bbl lot)"),
    # Forex (quantity in standard lots)
    InstrumentSpec("EURUSD", _X, FOREX_LOT, "USD", "Euro / US Dollar"),
    InstrumentSpec("GBPUSD", _X, FOREX_LOT, "USD", "British Pound / US Dollar"),
    InstrumentSpec("AUDUSD", _X, FOREX_LOT, "USD", "Australian Dollar / US Dollar"),
    InstrumentSpec("NZDUSD", _X, FOREX_LOT, "USD", "New Zealand Dollar / US Dollar"),
    InstrumentSpec("USDJPY", _X, FOREX_LOT, "JPY", "US Dollar / Japanese Yen"),
    InstrumentSpec("USDCAD", _X, FOREX_LOT, "CAD", "US Dollar / Canadian Dollar"),
    InstrumentSpec("USDCHF", _X, FOREX_LOT, "CHF", "US Dollar / Swiss Franc"),
    # Crypto (quantity in coins)
    InstrumentSpec("BTCUSD", _K, 1, "USD", "Bitcoin"),
    InstrumentSpec("ETHUSD", _K, 1, "USD", "Ether"),
    InstrumentSpec("SOLUSD", _K, 1, "USD", "Solana"),
]

CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK", "MXN",
              "SGD", "HKD", "ZAR", "TRY", "CNH", "PLN", "DKK"}  # fmt: skip
CRYPTO_BASES = {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LTC", "DOT", "LINK"}
STABLE_QUOTES = ("USDT", "USDC", "USD")
FUTURES_MONTHS = "FGHJKMNQUVXZ"

_FUTURES_CONTRACT = re.compile(rf"^([A-Z0-9]{{1,4}}?)[{FUTURES_MONTHS}](\d{{1,4}})$")


def normalise_symbol(raw: str) -> str:
    """Clean common broker/charting spellings: ``/ESZ6`` → ``ESZ6``, ``EUR/USD`` → ``EURUSD``,
    ``BTC-USD`` → ``BTCUSD``, ``ES1!`` → ``ES``, ``CL=F`` → ``CL``, ``BTCUSDT`` → ``BTCUSD``."""
    s = str(raw).strip().upper()
    s = s.removeprefix("/")
    s = re.sub(r"\d!$", "", s)  # TradingView continuous contracts
    s = s.removesuffix("=F").removesuffix("=X")  # Yahoo futures / forex
    s = s.replace("/", "").replace("-", "").replace("_", "").replace(" ", "")
    for quote in ("USDT", "USDC"):
        base = s.removesuffix(quote)
        if base != s and base in CRYPTO_BASES:
            return base + "USD"
    return s


def futures_root(symbol: str, known: Iterable[str]) -> str | None:
    """``ESZ6``/``ESZ26``/``MNQH2027`` → root if the root is a known contract."""
    match = _FUTURES_CONTRACT.match(symbol)
    if match and match.group(1) in set(known):
        return match.group(1)
    return None


def resolve(symbol: str, specs: dict[str, InstrumentSpec]) -> InstrumentSpec:
    """Find the spec for a (normalised) traded symbol.

    Order: exact match → futures contract root → forex pair pattern → crypto pair
    pattern → default US stock (multiplier 1).
    """
    sym = normalise_symbol(symbol)
    if sym in specs:
        return specs[sym]
    root = futures_root(sym, specs)
    if root is not None:
        return specs[root]
    if len(sym) == 6 and sym[:3] in CURRENCIES and sym[3:] in CURRENCIES:
        return InstrumentSpec(sym, AssetClass.FOREX, FOREX_LOT, sym[3:])
    for quote in STABLE_QUOTES:
        base = sym.removesuffix(quote)
        if base != sym and base in CRYPTO_BASES:
            return InstrumentSpec(sym, AssetClass.CRYPTO, 1.0, "USD")
    return InstrumentSpec(sym, AssetClass.STOCK, 1.0, "USD")


def point_value(spec: InstrumentSpec, price: float) -> float:
    """USD value of a 1.0 price move for one unit of quantity.

    For pairs quoted in another currency with USD as the base (USDJPY), the quote
    currency P&L is converted at ``price``. Other non-USD quotes (crosses such as
    EURGBP) are left unconverted.
    """
    if spec.quote_currency in STABLE_QUOTES:
        return spec.multiplier
    if spec.asset_class is AssetClass.FOREX and spec.symbol.startswith("USD") and price > 0:
        return spec.multiplier / price
    return spec.multiplier


def default_specs() -> dict[str, InstrumentSpec]:
    return {s.symbol: s for s in DEFAULT_INSTRUMENTS}
