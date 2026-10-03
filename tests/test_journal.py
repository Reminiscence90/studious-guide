from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.importer import Fill, import_fills
from core.journal import get_journal_entry, save_journal_entry, save_playbook
from core.models import ExecutionSide, JournalEntry, Trade, TradeChecklistResult
from core.trades import set_playbook


def test_one_journal_entry_per_day(session: Session) -> None:
    save_journal_entry(session, date(2026, 6, 1), pre_market_plan="plan", mood=4)
    save_journal_entry(session, date(2026, 6, 1), post_market_review="review", mood=2)
    entries = session.scalars(select(JournalEntry)).all()
    assert len(entries) == 1
    entry = get_journal_entry(session, date(2026, 6, 1))
    assert entry is not None
    assert (entry.pre_market_plan, entry.post_market_review, entry.mood) == ("", "review", 2)
    assert get_journal_entry(session, date(2026, 6, 2)) is None


def test_mood_is_validated(session: Session) -> None:
    with pytest.raises(ValueError):
        save_journal_entry(session, date(2026, 6, 1), mood=6)


def test_save_playbook_preserves_unchanged_checklist_items(session: Session) -> None:
    pb = save_playbook(session, name="ORB", checklist=["Range", "Volume", "Stop"])
    import_fills(
        session,
        1,
        [
            Fill("A", ExecutionSide.BUY, 1, 1, datetime(2026, 6, 1, 9)),
            Fill("A", ExecutionSide.SELL, 1, 2, datetime(2026, 6, 1, 10)),
        ],
    )
    trade = session.scalars(select(Trade)).one()
    range_id = pb.checklist_items[0].id
    set_playbook(session, trade, pb, [range_id])

    pb = save_playbook(
        session, playbook_id=pb.id, name="ORB v2", checklist=["Trend", "Range", "Stop", ""]
    )
    assert [i.text for i in pb.checklist_items] == ["Trend", "Range", "Stop"]
    assert pb.checklist_items[1].id == range_id
    results = session.scalars(select(TradeChecklistResult)).all()
    assert {(r.item_id, r.met) for r in results} == {
        (range_id, True),
        (pb.checklist_items[2].id, False),
    }


def test_save_playbook_rejects_duplicate_names(session: Session) -> None:
    save_playbook(session, name="ORB")
    with pytest.raises(ValueError):
        save_playbook(session, name="ORB")
    with pytest.raises(ValueError):
        save_playbook(session, name="  ")
