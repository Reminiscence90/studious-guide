from sqlalchemy import select
from sqlalchemy.orm import Session

from core.instruments import AssetClass
from core.models import Account, Instrument, Tag, TagCategory


def test_init_creates_default_account_tags_and_instruments(session: Session) -> None:
    accounts = session.scalars(select(Account)).all()
    assert [a.name for a in accounts] == ["Main"]
    setups = session.scalars(select(Tag.name).where(Tag.category == TagCategory.SETUP)).all()
    assert sorted(setups) == [
        "Golden Ratio - 0.618 retracement",
        "ICT - Liquidity Sweep + BOS + Imbalance",
        "Supply/Demand zone",
    ]
    es = session.scalars(select(Instrument).where(Instrument.symbol == "ES")).one()
    assert (es.asset_class, es.multiplier) == (AssetClass.FUTURE, 50)
    categories = {t.category for t in session.scalars(select(Tag))}
    assert categories == set(TagCategory)


def test_init_is_idempotent(engine) -> None:  # type: ignore[no-untyped-def]
    from core.db import init_db

    init_db(engine)
    with Session(engine) as s:
        assert len(s.scalars(select(Account)).all()) == 1


def test_deleting_account_cascades_to_trades(session: Session) -> None:
    from datetime import datetime

    from core.importer import Fill, import_fills
    from core.models import Execution, ExecutionSide, Trade

    account = Account(name="Temp")
    session.add(account)
    session.flush()
    import_fills(
        session,
        account.id,
        [
            Fill("A", ExecutionSide.BUY, 1, 1, datetime(2026, 1, 1, 9)),
            Fill("A", ExecutionSide.SELL, 1, 2, datetime(2026, 1, 1, 10)),
        ],
    )
    session.delete(session.get(Account, account.id))
    session.flush()
    assert session.scalars(select(Trade)).all() == []
    assert session.scalars(select(Execution)).all() == []


def test_init_db_adds_raw_symbol_to_older_databases() -> None:
    from sqlalchemy import create_engine, inspect, text

    from core.db import init_db

    engine = create_engine("sqlite://")
    with engine.begin() as conn:  # executions table as created by the first release
        conn.execute(
            text(
                "CREATE TABLE executions (id INTEGER PRIMARY KEY, account_id INTEGER, "
                "trade_id INTEGER, symbol VARCHAR(32), side VARCHAR(4), quantity FLOAT, "
                "price FLOAT, timestamp DATETIME, fees FLOAT, import_hash VARCHAR(64), "
                "source VARCHAR(32))"
            )
        )
    init_db(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("executions")}
    assert "raw_symbol" in columns
    init_db(engine)  # idempotent


def test_new_builtin_instruments_reach_an_existing_journal(engine) -> None:  # type: ignore[no-untyped-def]
    """A journal created before US500 existed: US500.R was a US stock until upgrade."""
    from datetime import datetime

    from core.db import init_db
    from core.importer import Fill, import_fills
    from core.models import ExecutionSide, Trade

    with Session(engine) as s:
        s.delete(s.scalars(select(Instrument).where(Instrument.symbol == "US500")).one())
        t0 = datetime(2026, 10, 20, 22, 22, 3)
        t1 = datetime(2026, 10, 20, 23, 12, 59)
        import_fills(
            s,
            1,
            [
                Fill("US500.R", ExecutionSide.SELL, 0.43, 7745.12, t0, 1.29, order_id="1"),
                Fill("US500.R", ExecutionSide.BUY, 0.43, 7745.01, t1, 1.29, order_id="2",
                     is_exit=True, realized_pnl=4.73, realized_net_pnl=2.15),
            ],
        )  # fmt: skip
        trade = s.scalars(select(Trade)).one()
        # Older versions did not know US500, so the suffix was kept.
        trade.symbol = "US500.R"
        for e in trade.executions:
            e.symbol = "US500.R"
        s.commit()
        assert trade.asset_class is AssetClass.STOCK

    init_db(engine)  # upgrade
    with Session(engine) as s:
        trade = s.scalars(select(Trade)).one()
        assert (trade.symbol, trade.asset_class) == ("US500", AssetClass.INDEX)
        assert trade.point_value == 100
        assert trade.net_pnl == 2.15
        assert {e.raw_symbol for e in trade.executions} == {"US500.R"}
        us500 = s.scalars(select(Instrument).where(Instrument.symbol == "US500")).one()
        assert us500.asset_class is AssetClass.INDEX
