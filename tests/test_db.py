from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import Account, Tag, TagCategory


def test_init_creates_default_account_and_tags(session: Session) -> None:
    accounts = session.scalars(select(Account)).all()
    assert [(a.name, a.currency) for a in accounts] == [("Main", "MYR")]
    categories = {t.category for t in session.scalars(select(Tag))}
    assert categories == set(TagCategory)


def test_init_is_idempotent(engine) -> None:  # type: ignore[no-untyped-def]
    from core.db import init_db

    init_db(engine)
    with Session(engine) as s:
        assert len(s.scalars(select(Account)).all()) == 1
