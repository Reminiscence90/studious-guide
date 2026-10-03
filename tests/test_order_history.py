"""Import of broker order-history exports (one row per order), e.g. Alchemy Markets."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.importer import (
    BUILTIN_PRESETS,
    ImportResult,
    detect_preset,
    guess_mapping,
    import_dataframe,
    recalculate_trades,
    rows_to_executions,
)
from core.instruments import AssetClass
from core.models import BrokerPreset, Execution, Trade, TradeSide, TradeStatus

CSV = Path(__file__).parent / "data" / "alchemy_order_history.csv"
PRESET = "Alchemy Markets – order history"
MAPPING, DATE_FORMAT = BUILTIN_PRESETS[PRESET]


@pytest.fixture
def df() -> pd.DataFrame:
    return pd.read_csv(CSV, dtype=str, keep_default_na=False)


@pytest.fixture
def imported(session: Session, df: pd.DataFrame) -> ImportResult:
    return import_dataframe(session, 1, df, MAPPING, DATE_FORMAT)


def _trade(session: Session, position_id: str) -> Trade:
    return session.scalars(
        select(Trade).where(Trade.executions.any(Execution.position_id == position_id))
    ).one()


def test_columns_are_recognised_automatically(df: pd.DataFrame) -> None:
    assert guess_mapping(df.columns) == MAPPING
    presets = {name: mapping for name, (mapping, _) in BUILTIN_PRESETS.items()}
    assert detect_preset(df.columns, presets) == PRESET
    assert detect_preset(["Ticker", "Qty"], presets) is None


def test_builtin_preset_is_installed(session: Session) -> None:
    preset = session.scalars(select(BrokerPreset).where(BrokerPreset.name == PRESET)).one()
    assert preset.mapping == MAPPING


def test_only_filled_orders_become_fills(df: pd.DataFrame) -> None:
    parsed = rows_to_executions(df, MAPPING, DATE_FORMAT)
    assert parsed.errors == []
    assert len(parsed.fills) == 14
    assert parsed.skipped == 7  # cancelled, working and rejected orders


def test_import_summary(imported: ImportResult) -> None:
    assert imported.rows_read == 21
    assert imported.rows_skipped == 7
    assert imported.executions_imported == 13  # the orphan closing fill is left out
    assert imported.trades_created == 7
    assert imported.errors == []
    assert len(imported.warnings) == 1
    assert "XAUUSD:1000" in imported.warnings[0].replace(".R", "")


def test_reimport_is_a_no_op(session: Session, df: pd.DataFrame, imported: ImportResult) -> None:
    again = import_dataframe(session, 1, df, MAPPING, DATE_FORMAT)
    assert (again.executions_imported, again.duplicates_skipped, again.trades_created) == (
        0,
        13,
        0,
    )


def test_entry_and_stop_loss_at_same_timestamp(session: Session, imported: ImportResult) -> None:
    trade = _trade(session, "XAUUSD.R:3000")
    assert (trade.symbol, trade.asset_class) == ("XAUUSD", AssetClass.COMMODITY)
    assert trade.side is TradeSide.SHORT
    assert trade.status is TradeStatus.CLOSED
    assert trade.gross_pnl == pytest.approx(-510.00)
    assert trade.net_pnl == pytest.approx(-511.50)
    assert trade.stop_loss == 4360  # from the filled stop-loss order
    assert {e.raw_symbol for e in trade.executions} == {"XAUUSD.R"}


def test_stop_from_cancelled_bracket_and_broker_net_pnl(
    session: Session, imported: ImportResult
) -> None:
    trade = _trade(session, "XAUUSD.R:4000")
    assert trade.stop_loss == 4290  # cancelled SL order 4002, linked by order ID
    assert trade.gross_pnl == pytest.approx(400.00)
    assert trade.fees == pytest.approx(0.60)
    assert trade.net_pnl == pytest.approx(401.00)  # broker net includes +1.60 swap


def test_hedged_positions_in_same_symbol_stay_separate(
    session: Session, imported: ImportResult
) -> None:
    first, second = _trade(session, "XAUUSD.R:5000"), _trade(session, "XAUUSD.R:5100")
    assert first.id != second.id
    assert (first.net_pnl, second.net_pnl) == (pytest.approx(49.70), pytest.approx(-40.30))


def test_breakeven_stop_is_not_used_as_initial_risk(
    session: Session, imported: ImportResult
) -> None:
    trade = _trade(session, "XAUUSD.R:6000")
    assert trade.stop_loss is None  # stop at 4199.8 is on the profit side of a 4200 short


def test_cross_pair_uses_broker_pnl_in_usd(session: Session, imported: ImportResult) -> None:
    trade = _trade(session, "EURJPY.R:7000")
    assert trade.asset_class is AssetClass.FOREX
    assert trade.gross_pnl == pytest.approx(170.00)  # not 0.5 × 100,000 × 0.5 JPY
    assert trade.net_pnl == pytest.approx(167.00)


def test_open_position_and_its_working_stop(session: Session, imported: ImportResult) -> None:
    trade = _trade(session, "AUDUSD.R:8000")
    assert trade.status is TradeStatus.OPEN
    assert trade.stop_loss == pytest.approx(0.6950)


def test_open_position_is_closed_by_a_later_export(
    session: Session, imported: ImportResult
) -> None:
    later = pd.DataFrame(
        [
            {**dict.fromkeys(MAPPING.values(), ""), "Symbol": "AUDUSD.R", "Side": "Sell",
             "Type": "Take Profit", "Filled Qty": "1", "Avg Fill Price": "0.70500",
             "Status": "Filled", "Update Time": "2026-09-23 01:00:00",
             "Position ID": "AUDUSD.R:8000", "Commission": "-3.0", "Closed P&L": "500.00",
             "Net Closed P&L": "494.00", "Order ID": "8001"},
        ]
    )  # fmt: skip
    result = import_dataframe(session, 1, later, MAPPING, DATE_FORMAT)
    assert (result.trades_updated, result.trades_created) == (1, 0)
    trade = _trade(session, "AUDUSD.R:8000")
    assert trade.status is TradeStatus.CLOSED
    assert trade.net_pnl == pytest.approx(494.00)
    assert trade.stop_loss == pytest.approx(0.6950)


def test_orphan_is_imported_once_its_opening_fill_arrives(
    session: Session, df: pd.DataFrame, imported: ImportResult
) -> None:
    opening = pd.DataFrame(
        [
            {**dict.fromkeys(MAPPING.values(), ""), "Symbol": "XAUUSD.R", "Side": "Sell",
             "Type": "Limit", "Filled Qty": "0.19", "Avg Fill Price": "4334.97",
             "Status": "Filled", "Update Time": "2026-09-16 20:00:00",
             "Position ID": "XAUUSD.R:1000", "Commission": "-0.57", "Order ID": "1000"},
        ]
    )  # fmt: skip
    longer_export = pd.concat([opening, df], ignore_index=True)
    result = import_dataframe(session, 1, longer_export, MAPPING, DATE_FORMAT)
    assert result.warnings == []
    assert result.executions_imported == 2
    trade = _trade(session, "XAUUSD.R:1000")
    assert trade.status is TradeStatus.CLOSED
    assert trade.net_pnl == pytest.approx(-513.19)


def test_broker_pnl_survives_recalculation(session: Session, imported: ImportResult) -> None:
    recalculate_trades(session)
    trade = _trade(session, "EURJPY.R:7000")
    session.refresh(trade)
    assert trade.net_pnl == pytest.approx(167.00)


def test_totals_match_the_broker(session: Session, imported: ImportResult) -> None:
    closed = session.scalars(select(Trade).where(Trade.status == TradeStatus.CLOSED)).all()
    assert sum(t.net_pnl for t in closed) == pytest.approx(-511.50 + 401 + 49.7 - 40.3 + 2.1 + 167)
