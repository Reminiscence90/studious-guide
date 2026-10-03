from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from core import metrics as m
from core.importer import Fill, import_fills
from core.models import ExecutionSide, Tag, TagCategory, Trade
from core.repository import load_trades

BUY, SELL = ExecutionSide.BUY, ExecutionSide.SELL


@pytest.fixture
def trades(session: Session) -> Session:
    import_fills(
        session,
        1,
        [
            Fill("A", BUY, 100, 10, datetime(2026, 6, 1, 9, 30)),
            Fill("A", SELL, 100, 11, datetime(2026, 6, 1, 10, 0)),
            Fill("B", SELL, 100, 20, datetime(2026, 6, 2, 9, 30)),
            Fill("B", BUY, 100, 21, datetime(2026, 6, 3, 9, 30)),
            Fill("C", BUY, 10, 5, datetime(2026, 6, 4, 9, 30)),  # stays open
        ],
    )
    trade = session.query(Trade).filter_by(symbol="A").one()
    trade.stop_loss = 9.5
    trade.tags.append(session.query(Tag).filter_by(name="Breakout").one())
    trade.tags.append(Tag(name="Patient", category=TagCategory.EMOTION))
    session.flush()
    return session


def test_load_trades_columns_and_derived_fields(trades: Session) -> None:
    df = load_trades(trades)
    assert len(df) == 3
    a = df.set_index("symbol").loc["A"]
    assert a["setups"] == ["Breakout"]
    assert a["emotions"] == ["Patient"]
    assert a["r_multiple"] == pytest.approx(2.0)
    assert a["currency"] == "MYR"
    assert a["holding_bucket"] == "15–60 min"
    assert m.total_trades(df) == 2
    assert m.net_pnl(df) == pytest.approx(0)


def test_date_filter_uses_exit_date(trades: Session) -> None:
    df = load_trades(trades, start=date(2026, 6, 2), end=date(2026, 6, 3))
    assert df["symbol"].tolist() == ["B"]  # B closed on 3 June; C is open from 4 June
    df = load_trades(trades, start=date(2026, 6, 4))
    assert df["symbol"].tolist() == ["C"]


def test_exclude_open_and_account_filter(trades: Session) -> None:
    assert len(load_trades(trades, include_open=False)) == 2
    assert load_trades(trades, account_ids=[999]).empty


def test_empty_database(session: Session) -> None:
    df = load_trades(session)
    assert df.empty
    assert m.summary_stats(df).total_trades == 0
