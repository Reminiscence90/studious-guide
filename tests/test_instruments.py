import pytest

from core.instruments import (
    AssetClass,
    InstrumentSpec,
    default_specs,
    normalise_symbol,
    point_value,
    resolve,
)

SPECS = default_specs()


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("tsla", "TSLA"),
        ("/ESZ6", "ESZ6"),
        ("EUR/USD", "EURUSD"),
        ("BTC-USD", "BTCUSD"),
        ("BTCUSDT", "BTCUSD"),
        ("ethusdc", "ETHUSD"),
        ("ES1!", "ES"),
        ("CL=F", "CL"),
        (" xauusd ", "XAUUSD"),
        # broker account suffixes are dropped from market symbols...
        ("XAUUSD.R", "XAUUSD"),
        ("EURUSD.R", "EURUSD"),
        ("eurusd.r", "EURUSD"),
        ("GBPJPY.pro", "GBPJPY"),
        ("USOIL.m", "USOIL"),
        ("BTCUSDT.R", "BTCUSD"),
        ("EUR/USD.R", "EURUSD"),
        # ...but not from other dotted symbols
        ("BRK.B", "BRK.B"),
        ("FOO.R", "FOO.R"),
    ],
)
def test_normalise_symbol(raw: str, clean: str) -> None:
    assert normalise_symbol(raw) == clean


def test_suffix_dropped_for_user_defined_instrument() -> None:
    assert normalise_symbol("US30.R") == "US30.R"
    assert normalise_symbol("US30.R", known={"US30"}) == "US30"


def test_suffixed_symbols_resolve_to_base_spec() -> None:
    assert resolve("XAUUSD.R", SPECS) == SPECS["XAUUSD"]
    assert resolve("EURUSD.R", SPECS) == SPECS["EURUSD"]


@pytest.mark.parametrize(
    ("symbol", "asset", "multiplier"),
    [
        ("TSLA", AssetClass.STOCK, 1),
        ("ESZ6", AssetClass.FUTURE, 50),
        ("ESZ26", AssetClass.FUTURE, 50),
        ("MESH7", AssetClass.FUTURE, 5),
        ("MNQH2027", AssetClass.FUTURE, 2),
        ("6EM6", AssetClass.FUTURE, 125_000),
        ("GCQ6", AssetClass.COMMODITY, 100),
        ("MCLV6", AssetClass.COMMODITY, 100),
        ("XAUUSD", AssetClass.COMMODITY, 100),
        ("EURUSD", AssetClass.FOREX, 100_000),
        ("EURGBP", AssetClass.FOREX, 100_000),  # unlisted pair found by pattern
        ("BTCUSD", AssetClass.CRYPTO, 1),
        ("DOGEUSD", AssetClass.CRYPTO, 1),  # unlisted crypto found by pattern
        ("PLTR", AssetClass.STOCK, 1),  # unknown → US stock
        ("ESPR", AssetClass.STOCK, 1),  # looks nothing like a contract code
    ],
)
def test_resolve(symbol: str, asset: AssetClass, multiplier: float) -> None:
    spec = resolve(symbol, SPECS)
    assert (spec.asset_class, spec.multiplier) == (asset, multiplier)


def test_point_value_conversion() -> None:
    assert point_value(SPECS["EURUSD"], 1.17) == 100_000
    assert point_value(SPECS["USDJPY"], 150.0) == pytest.approx(100_000 / 150)
    assert point_value(SPECS["ES"], 6500) == 50
    # Crosses without USD are left in the quote currency (documented limitation).
    assert point_value(InstrumentSpec("EURGBP", AssetClass.FOREX, 100_000, "GBP"), 0.86) == 100_000


def test_asset_class_labels() -> None:
    assert [a.label for a in AssetClass] == [
        "US stocks", "Futures", "Forex", "Commodities", "Crypto"
    ]  # fmt: skip
