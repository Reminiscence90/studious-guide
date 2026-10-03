from __future__ import annotations

from collections import Counter
from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.importer import (
    Fill,
    ImportError_,
    add_manual_trade,
    assign_fingerprints,
    group_executions,
    guess_mapping,
    import_dataframe,
    import_fills,
    load_specs,
    parse_side,
    recalculate_trades,
    replace_instruments,
    rows_to_executions,
    save_preset,
    validate_mapping,
)
from core.instruments import AssetClass, InstrumentSpec, normalise_symbol
from core.models import (
    Account,
    BrokerPreset,
    Execution,
    ExecutionSide,
    Trade,
    TradeSide,
    TradeStatus,
)

BUY, SELL = ExecutionSide.BUY, ExecutionSide.SELL


def t(hhmm: str, day: int = 1) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 6, day, h, m)


def fill(side: ExecutionSide, qty: float, price: float, when: str, sym: str = "ABC") -> Fill:
    return Fill(sym, side, qty, price, t(when))


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize("raw", ["BUY", "buy", " Long ", "B", "BTO", "Bot"])
def test_parse_side_buy(raw: str) -> None:
    assert parse_side(raw) is BUY


@pytest.mark.parametrize("raw", ["SELL", "Short", "s", "SS", "STC", "Sld"])
def test_parse_side_sell(raw: str) -> None:
    assert parse_side(raw) is SELL


def test_parse_side_rejects_unknown() -> None:
    with pytest.raises(ImportError_):
        parse_side("maybe")


def test_guess_mapping_recognises_common_headers() -> None:
    cols = ["Ticker", "Direction", "Qty", "Entry Price", "Open Time", "Exit Price",
            "Close Time", "Commission"]  # fmt: skip
    assert guess_mapping(cols) == {
        "symbol": "Ticker",
        "side": "Direction",
        "quantity": "Qty",
        "entry_price": "Entry Price",
        "entry_time": "Open Time",
        "exit_price": "Exit Price",
        "exit_time": "Close Time",
        "fees": "Commission",
    }


def test_validate_mapping_reports_missing_and_half_exit() -> None:
    problems = validate_mapping({"symbol": "a", "exit_price": "x"}, ["a", "x"])
    assert any("Side" in p for p in problems)
    assert any("exit" in p.lower() for p in problems)


def test_rows_to_executions_round_trip_rows_become_two_fills() -> None:
    df = pd.DataFrame(
        {
            "Sym": ["abc"],
            "Side": ["Short"],
            "Qty": ["1,000"],
            "In": [10.0],
            "InT": ["2026-06-01 09:30"],
            "Out": [9.5],
            "OutT": ["2026-06-01 10:00"],
            "Fee": [4.0],
        }
    )
    mapping = {
        "symbol": "Sym",
        "side": "Side",
        "quantity": "Qty",
        "entry_price": "In",
        "entry_time": "InT",
        "exit_price": "Out",
        "exit_time": "OutT",
        "fees": "Fee",
    }
    fills, errors = rows_to_executions(df, mapping)
    assert errors == []
    assert [(f.symbol, f.side, f.quantity, f.price, f.fees) for f in fills] == [
        ("ABC", SELL, 1000.0, 10.0, 2.0),
        ("ABC", BUY, 1000.0, 9.5, 2.0),
    ]


def test_rows_to_executions_collects_row_errors() -> None:
    df = pd.DataFrame(
        {"s": ["A", "B"], "d": ["buy", "??"], "q": [1, 1], "p": [1, 1], "t": ["2026-01-01"] * 2}
    )
    mapping = {"symbol": "s", "side": "d", "quantity": "q", "entry_price": "p", "entry_time": "t"}
    fills, errors = rows_to_executions(df, mapping)
    assert len(fills) == 1
    assert errors == ["Row 3: Unrecognised side value: '??'"]


def test_rows_to_executions_with_explicit_datetime_format() -> None:
    df = pd.DataFrame({"s": ["A"], "d": ["B"], "q": [1], "p": [2], "t": ["03/06/2026 14:05"]})
    mapping = {"symbol": "s", "side": "d", "quantity": "q", "entry_price": "p", "entry_time": "t"}
    fills, _ = rows_to_executions(df, mapping, "%d/%m/%Y %H:%M")
    assert fills[0].timestamp == datetime(2026, 6, 3, 14, 5)


# ---------------------------------------------------------------- grouping


def test_partial_fills_grouped_into_single_trade() -> None:
    groups = group_executions(
        [
            fill(BUY, 100, 10.0, "09:30"),
            fill(BUY, 100, 10.2, "09:35"),
            fill(SELL, 50, 10.5, "10:00"),
            fill(SELL, 150, 10.6, "10:30"),
        ]
    )
    assert len(groups) == 1
    s = groups[0].summary()
    assert s.side is TradeSide.LONG
    assert s.status is TradeStatus.CLOSED
    assert s.quantity == 200
    assert s.avg_entry_price == pytest.approx(10.1)
    assert s.avg_exit_price == pytest.approx(10.575)
    assert s.gross_pnl == pytest.approx((10.575 - 10.1) * 200)
    assert s.entry_time == t("09:30")
    assert s.exit_time == t("10:30")


def test_returning_to_flat_starts_a_new_trade() -> None:
    groups = group_executions(
        [
            fill(BUY, 100, 10, "09:30"),
            fill(SELL, 100, 11, "09:40"),
            fill(BUY, 100, 12, "09:50"),
            fill(SELL, 100, 11, "10:00"),
        ]
    )
    assert [g.summary().gross_pnl for g in groups] == [100, -100]


def test_short_trade_pnl() -> None:
    (g,) = group_executions([fill(SELL, 10, 50, "09:30"), fill(BUY, 10, 45, "09:45")])
    s = g.summary()
    assert s.side is TradeSide.SHORT
    assert s.gross_pnl == pytest.approx(50)


def test_fees_reduce_net_pnl() -> None:
    fills = [
        Fill("ABC", BUY, 10, 10, t("09:30"), fees=1.5),
        Fill("ABC", SELL, 10, 11, t("09:31"), fees=1.5),
    ]
    s = group_executions(fills)[0].summary()
    assert s.gross_pnl == pytest.approx(10)
    assert s.fees == pytest.approx(3)
    assert s.net_pnl == pytest.approx(7)


def test_unsorted_input_is_processed_in_time_order() -> None:
    groups = group_executions([fill(SELL, 100, 11, "10:00"), fill(BUY, 100, 10, "09:00")])
    assert len(groups) == 1
    assert groups[0].side is TradeSide.LONG


def test_open_position_is_marked_open() -> None:
    (g,) = group_executions([fill(BUY, 100, 10, "09:30"), fill(SELL, 40, 11, "09:40")])
    s = g.summary()
    assert s.status is TradeStatus.OPEN
    assert s.exit_time is None
    assert s.gross_pnl == pytest.approx(40)  # realised on the closed 40 shares


def test_position_flip_is_split_into_two_trades() -> None:
    fills = [
        Fill("ABC", BUY, 100, 10, t("09:30")),
        Fill("ABC", SELL, 150, 11, t("09:40"), fees=3.0),
        Fill("ABC", BUY, 50, 10, t("09:50")),
    ]
    first, second = group_executions(fills)
    assert first.side is TradeSide.LONG and first.is_closed
    assert first.fills[-1].quantity == 100
    assert first.fills[-1].fees == pytest.approx(2.0)
    assert second.side is TradeSide.SHORT and second.is_closed
    assert second.fills[0].quantity == 50
    assert second.fills[0].fees == pytest.approx(1.0)
    assert second.summary().gross_pnl == pytest.approx(50)


def test_symbols_are_grouped_independently() -> None:
    groups = group_executions(
        [
            fill(BUY, 1, 10, "09:30", "AAA"),
            fill(BUY, 1, 20, "09:31", "BBB"),
            fill(SELL, 1, 11, "09:32", "AAA"),
            fill(SELL, 1, 19, "09:33", "BBB"),
        ]
    )
    assert {g.symbol: g.summary().gross_pnl for g in groups} == {"AAA": 1, "BBB": -1}


def test_fingerprints_distinguish_identical_rows_within_a_batch() -> None:
    f = fill(BUY, 1, 10, "09:30")
    hashed = assign_fingerprints(1, [f, f])
    assert hashed[0].import_hash != hashed[1].import_hash
    # ...but are stable across batches
    assert assign_fingerprints(1, [f, f]) == hashed
    assert assign_fingerprints(2, [f])[0].import_hash != hashed[0].import_hash


# ---------------------------------------------------------------- persistence


def _csv(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


MAPPING = {
    "symbol": "sym",
    "side": "side",
    "quantity": "qty",
    "entry_price": "price",
    "entry_time": "time",
    "fees": "fee",
}


def test_import_skips_duplicates(session: Session) -> None:
    df = _csv(
        [
            {
                "sym": "A",
                "side": "buy",
                "qty": 1,
                "price": 10,
                "time": "2026-06-01 09:30",
                "fee": 0,
            },
            {
                "sym": "A",
                "side": "sell",
                "qty": 1,
                "price": 12,
                "time": "2026-06-01 09:40",
                "fee": 0,
            },
        ]
    )
    first = import_dataframe(session, 1, df, MAPPING)
    assert (first.executions_imported, first.trades_created) == (2, 1)
    second = import_dataframe(session, 1, df, MAPPING)
    assert (second.executions_imported, second.duplicates_skipped) == (0, 2)
    assert len(session.scalars(select(Trade)).all()) == 1


def test_same_rows_in_another_account_are_not_duplicates(session: Session) -> None:
    other = Account(name="Other")
    session.add(other)
    session.flush()
    df = _csv([{"sym": "A", "side": "buy", "qty": 1, "price": 10, "time": "2026-06-01", "fee": 0}])
    import_dataframe(session, 1, df, MAPPING)
    result = import_dataframe(session, other.id, df, MAPPING)
    assert result.executions_imported == 1


def test_open_trade_is_continued_by_a_later_import(session: Session) -> None:
    opening = _csv(
        [
            {
                "sym": "A",
                "side": "buy",
                "qty": 2,
                "price": 10,
                "time": "2026-06-01 09:30",
                "fee": 1,
            },
            {
                "sym": "A",
                "side": "sell",
                "qty": 1,
                "price": 11,
                "time": "2026-06-01 09:40",
                "fee": 1,
            },
        ]
    )
    import_dataframe(session, 1, opening, MAPPING)
    trade = session.scalars(select(Trade)).one()
    assert trade.status is TradeStatus.OPEN
    trade.notes = "keep me"
    session.flush()

    closing = _csv(
        [
            {
                "sym": "A",
                "side": "sell",
                "qty": 1,
                "price": 12,
                "time": "2026-06-02 10:00",
                "fee": 1,
            },
            {"sym": "A", "side": "buy", "qty": 5, "price": 9, "time": "2026-06-03 10:00", "fee": 0},
        ]
    )
    result = import_dataframe(session, 1, closing, MAPPING)
    assert (result.trades_updated, result.trades_created) == (1, 1)
    trades = session.scalars(select(Trade).order_by(Trade.entry_time)).all()
    assert trades[0].id == trade.id
    assert trades[0].notes == "keep me"
    assert trades[0].status is TradeStatus.CLOSED
    assert trades[0].net_pnl == pytest.approx(3 - 3)
    assert len(trades[0].executions) == 3
    assert trades[1].status is TradeStatus.OPEN
    assert session.scalars(select(Execution)).all().__len__() == 4


def test_manual_trade(session: Session) -> None:
    trade = add_manual_trade(
        session,
        1,
        symbol=" tsla ",
        side=TradeSide.SHORT,
        quantity=100,
        entry_price=10,
        entry_time=t("09:30"),
        exit_price=9,
        exit_time=t("10:30"),
        fees=2,
        stop_loss=10.5,
        notes="manual",
    )
    assert trade is not None
    assert trade.symbol == "TSLA"
    assert trade.net_pnl == pytest.approx(98)
    assert trade.stop_loss == 10.5
    assert trade.notes == "manual"
    assert {e.source for e in trade.executions} == {"manual"}


def test_manual_trade_validation(session: Session) -> None:
    with pytest.raises(ImportError_):
        add_manual_trade(
            session,
            1,
            symbol="A",
            side=TradeSide.LONG,
            quantity=1,
            entry_price=1,
            entry_time=t("10:00"),
            exit_price=2,
            exit_time=t("09:00"),
        )


def test_import_fills_with_no_fills(session: Session) -> None:
    result = import_fills(session, 1, [])
    assert result.executions_imported == 0


def test_save_preset_overwrites(session: Session) -> None:
    save_preset(session, "Broker", {"symbol": "A", "side": None})
    save_preset(session, "Broker", {"symbol": "B"}, "%Y")
    preset = session.scalars(select(BrokerPreset)).one()
    assert preset.mapping == {"symbol": "B"}
    assert preset.datetime_format == "%Y"


def test_sample_csv_imports_cleanly(session: Session) -> None:
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "sample_data" / "sample_trades.csv"
    df = pd.read_csv(path)
    result = import_dataframe(session, 1, df, guess_mapping(df.columns))
    assert result.errors == []
    trades = session.scalars(select(Trade)).all()
    assert 190 <= len(trades) <= 220
    assert len(trades) < len(df)  # partial exits were grouped
    assert sum(tr.status is TradeStatus.OPEN for tr in trades) == 2
    assert {tr.asset_class for tr in trades} == set(AssetClass)
    by_instrument = Counter(tr.instrument for tr in trades)
    assert by_instrument.most_common(1)[0][0] == "TSLA"
    es = next(tr for tr in trades if tr.symbol.startswith("ES") and tr.symbol != "ES")
    assert (es.instrument, es.point_value) == ("ES", 50)


# ---------------------------------------------------------------- multipliers


def _round_trip(session: Session, symbol: str, side: ExecutionSide, qty: float,
                entry: float, exit_: float) -> Trade:  # fmt: skip
    import_fills(
        session,
        1,
        [
            Fill(symbol, side, qty, entry, t("09:30")),
            Fill(symbol, SELL if side is BUY else BUY, qty, exit_, t("10:30")),
        ],
    )
    return session.scalars(select(Trade).where(Trade.symbol == normalise_symbol(symbol))).one()


def test_futures_pnl_uses_contract_multiplier(session: Session) -> None:
    trade = _round_trip(session, "ESZ6", BUY, 2, 6500.00, 6510.25)
    assert (trade.instrument, trade.asset_class) == ("ES", AssetClass.FUTURE)
    assert trade.gross_pnl == pytest.approx(10.25 * 2 * 50)


def test_micro_futures_and_commodity_futures(session: Session) -> None:
    assert _round_trip(session, "MNQH7", SELL, 3, 23500, 23450).gross_pnl == pytest.approx(300)
    cl = _round_trip(session, "CLX6", BUY, 1, 65.10, 65.55)
    assert cl.asset_class is AssetClass.COMMODITY
    assert cl.gross_pnl == pytest.approx(450)


def test_forex_lots_usd_quoted(session: Session) -> None:
    trade = _round_trip(session, "EUR/USD", BUY, 0.5, 1.16500, 1.16700)
    assert trade.symbol == "EURUSD"
    assert trade.gross_pnl == pytest.approx(0.002 * 0.5 * 100_000)  # 20 pips × 0.5 lot = $100


def test_forex_usd_base_pair_converted_to_usd(session: Session) -> None:
    trade = _round_trip(session, "USDJPY", SELL, 1, 148.000, 147.500)
    # 50 pips on 1 lot = ¥50,000, converted at the exit rate 147.5
    assert trade.gross_pnl == pytest.approx(50_000 / 147.5)
    assert trade.point_value == pytest.approx(100_000 / 147.5)


def test_spot_gold_and_crypto(session: Session) -> None:
    gold = _round_trip(session, "XAUUSD", BUY, 0.2, 3400.0, 3412.5)
    assert gold.gross_pnl == pytest.approx(12.5 * 0.2 * 100)
    btc = _round_trip(session, "BTC-USDT", SELL, 0.05, 112_000, 111_000)
    assert (btc.symbol, btc.asset_class) == ("BTCUSD", AssetClass.CRYPTO)
    assert btc.gross_pnl == pytest.approx(50)


def test_broker_suffix_is_treated_as_base_symbol(session: Session) -> None:
    # Opened as XAUUSD.R, closed as XAUUSD: one position, one trade.
    df = _csv(
        [
            {"sym": "XAUUSD.R", "side": "buy", "qty": 0.5, "price": 3400.0,
             "time": "2026-06-01 09:30", "fee": 0},
            {"sym": "XAUUSD", "side": "sell", "qty": 0.5, "price": 3410.0,
             "time": "2026-06-01 10:00", "fee": 0},
            {"sym": "EURUSD.R", "side": "sell", "qty": 1, "price": 1.1700,
             "time": "2026-06-01 11:00", "fee": 0},
            {"sym": "EURUSD.r", "side": "buy", "qty": 1, "price": 1.1650,
             "time": "2026-06-01 12:00", "fee": 0},
        ]
    )  # fmt: skip
    result = import_dataframe(session, 1, df, MAPPING)
    assert result.trades_created == 2
    trades = {tr.symbol: tr for tr in session.scalars(select(Trade))}
    assert set(trades) == {"XAUUSD", "EURUSD"}
    assert trades["XAUUSD"].gross_pnl == pytest.approx(10 * 0.5 * 100)
    assert trades["EURUSD"].gross_pnl == pytest.approx(0.005 * 100_000)
    assert trades["EURUSD"].asset_class is AssetClass.FOREX
    # Suffixed symbols are classed like their base: gold is a commodity, EURUSD forex.
    assert trades["XAUUSD"].asset_class is AssetClass.COMMODITY
    assert trades["XAUUSD"].point_value == 100
    raw = {e.raw_symbol for e in session.scalars(select(Execution))}
    assert raw == {"XAUUSD.R", "XAUUSD", "EURUSD.R"}  # as imported, upper-cased


def test_suffixed_gold_is_a_commodity_and_keeps_raw_symbol(session: Session) -> None:
    gold = _round_trip(session, "XAUUSD.R", BUY, 0.1, 3400.0, 3405.0)
    assert (gold.symbol, gold.asset_class) == ("XAUUSD", AssetClass.COMMODITY)
    assert {e.raw_symbol for e in gold.executions} == {"XAUUSD.R"}
    recalculate_trades(session)
    session.refresh(gold)
    assert gold.asset_class is AssetClass.COMMODITY
    assert gold.gross_pnl == pytest.approx(5 * 0.1 * 100)


def test_recalculation_fixes_previously_misclassified_trades(session: Session) -> None:
    gold = _round_trip(session, "XAUUSD.R", BUY, 0.1, 3400.0, 3405.0)
    gold.asset_class = AssetClass.FOREX  # as stored by the earlier suffix rule
    session.flush()
    recalculate_trades(session)
    session.refresh(gold)
    assert gold.asset_class is AssetClass.COMMODITY


def test_manual_trade_with_broker_suffix(session: Session) -> None:
    trade = add_manual_trade(
        session,
        1,
        symbol="eurusd.r",
        side=TradeSide.LONG,
        quantity=1,
        entry_price=1.1600,
        entry_time=t("09:30"),
        exit_price=1.1625,
        exit_time=t("10:30"),
        stop_loss=1.1580,
    )
    assert trade is not None
    assert (trade.symbol, trade.asset_class, trade.stop_loss) == (
        "EURUSD",
        AssetClass.FOREX,
        1.1580,
    )
    assert trade.gross_pnl == pytest.approx(250)


def test_unknown_symbol_is_a_us_stock(session: Session) -> None:
    trade = _round_trip(session, "TSLA", BUY, 100, 430.0, 433.5)
    assert (trade.asset_class, trade.point_value) == (AssetClass.STOCK, 1)
    assert trade.gross_pnl == pytest.approx(350)


def test_recalculate_after_changing_multiplier(session: Session) -> None:
    trade = _round_trip(session, "FOO", BUY, 10, 100, 101)
    assert trade.gross_pnl == pytest.approx(10)
    specs = list(load_specs(session).values())
    replace_instruments(session, [*specs, InstrumentSpec("FOO", AssetClass.FUTURE, 20)])
    assert recalculate_trades(session) == 1
    session.refresh(trade)
    assert (trade.gross_pnl, trade.asset_class) == (pytest.approx(200), AssetClass.FUTURE)


def test_replace_instruments_validation(session: Session) -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        replace_instruments(
            session,
            [
                InstrumentSpec("es", AssetClass.FUTURE, 50),
                InstrumentSpec("ES", AssetClass.FUTURE, 5),
            ],
        )
    with pytest.raises(ValueError, match="positive"):
        replace_instruments(session, [InstrumentSpec("ES", AssetClass.FUTURE, 0)])
