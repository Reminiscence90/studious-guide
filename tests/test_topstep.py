"""Import of the Topstep trades export (one row per round trip)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.importer import (
    BUILTIN_PRESETS,
    detect_preset,
    guess_mapping,
    import_dataframe,
    recalculate_trades,
    rows_to_executions,
)
from core.instruments import AssetClass
from core.models import BrokerPreset, Execution, Trade, TradeSide, TradeStatus

CSV = Path(__file__).parent / "data" / "topstep_trades.csv"
PRESET = "Topstep – trades"
MAPPING, DATE_FORMAT = BUILTIN_PRESETS[PRESET]


@pytest.fixture
def df() -> pd.DataFrame:
    return pd.read_csv(CSV, dtype=str, keep_default_na=False)


def _trades(session: Session) -> list[Trade]:
    return list(session.scalars(select(Trade).order_by(Trade.entry_time)))


def test_columns_are_recognised_automatically(df: pd.DataFrame) -> None:
    # A plain "PnL" column is not guessed (it may be net elsewhere); the preset maps it.
    assert guess_mapping(df.columns) == {k: v for k, v in MAPPING.items() if k != "closed_pnl"}
    presets = {name: mapping for name, (mapping, _) in BUILTIN_PRESETS.items()}
    assert detect_preset(df.columns, presets) == PRESET


def test_builtin_preset_is_installed(session: Session) -> None:
    preset = session.scalars(select(BrokerPreset).where(BrokerPreset.name == PRESET)).one()
    assert preset.mapping == MAPPING
    assert preset.datetime_format == DATE_FORMAT


def test_each_row_is_two_fills_with_fees_and_commissions(df: pd.DataFrame) -> None:
    parsed = rows_to_executions(df, MAPPING, DATE_FORMAT)
    assert parsed.errors == []
    assert len(parsed.fills) == 10
    entry, exit_ = parsed.fills[:2]
    assert entry.order_id == "1000000001:entry"
    assert exit_.order_id == "1000000001:exit"
    assert entry.position_id == exit_.position_id == "1000000001"
    assert entry.fees + exit_.fees == pytest.approx(3.78)  # 2.78 fees + 1.00 commission
    # The UTC offset is dropped; the time is kept as exported.
    assert entry.timestamp == datetime(2026, 10, 20, 16, 44, 56)


def test_import_uses_topstep_pnl_and_contract_multipliers(
    session: Session, df: pd.DataFrame
) -> None:
    result = import_dataframe(session, 1, df, MAPPING, DATE_FORMAT)
    assert result.errors == []
    assert result.trades_created == 5
    trades = _trades(session)
    assert [t.instrument for t in trades] == ["NQ", "NQ", "MNQ", "MNQ", "MGC"]
    assert [t.side for t in trades] == [
        TradeSide.SHORT,
        TradeSide.LONG,
        TradeSide.LONG,
        TradeSide.SHORT,
        TradeSide.SHORT,
    ]
    assert all(t.status is TradeStatus.CLOSED for t in trades)
    assert [t.gross_pnl for t in trades] == pytest.approx([1000, -300, 60, 0, 36])
    assert [t.net_pnl for t in trades] == pytest.approx([996.22, -303.78, 56.34, -1.22, 30.24])
    assert sum(t.net_pnl for t in trades) == pytest.approx(777.80)
    assert trades[4].asset_class is AssetClass.COMMODITY
    assert trades[0].asset_class is AssetClass.FUTURE
    assert trades[0].point_value == 20
    assert trades[0].exit_time == datetime(2026, 10, 20, 16, 56, 21)


def test_reimport_skips_every_row(session: Session, df: pd.DataFrame) -> None:
    import_dataframe(session, 1, df, MAPPING, DATE_FORMAT)
    again = import_dataframe(session, 1, df, MAPPING, DATE_FORMAT)
    assert again.executions_imported == 0
    assert again.duplicates_skipped == 10
    assert len(_trades(session)) == 5


def test_recalculation_keeps_broker_pnl(session: Session, df: pd.DataFrame) -> None:
    import_dataframe(session, 1, df, MAPPING, DATE_FORMAT)
    recalculate_trades(session)
    assert sum(t.net_pnl for t in _trades(session)) == pytest.approx(777.80)
    assert session.scalar(select(Execution.order_id).limit(1)).endswith(":entry")
