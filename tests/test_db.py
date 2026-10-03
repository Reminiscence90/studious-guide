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
