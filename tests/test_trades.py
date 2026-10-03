from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.importer import Fill, import_fills
from core.models import (
    ExecutionSide,
    Playbook,
    PlaybookChecklistItem,
    Screenshot,
    Tag,
    TagCategory,
    Trade,
)
from core.trades import (
    add_screenshot,
    delete_screenshot,
    delete_trade,
    format_duration,
    set_playbook,
    set_trade_tags,
)


@pytest.fixture
def trade(session: Session) -> Trade:
    import_fills(
        session,
        1,
        [
            Fill("A", ExecutionSide.BUY, 1, 10, datetime(2026, 6, 1, 9)),
            Fill("A", ExecutionSide.SELL, 1, 12, datetime(2026, 6, 1, 10)),
        ],
    )
    return session.scalars(select(Trade)).one()


def test_set_trade_tags_replaces_per_category_and_creates_custom(
    session: Session, trade: Trade
) -> None:
    set_trade_tags(
        session,
        trade,
        {TagCategory.SETUP: ["Supply/Demand zone"], TagCategory.EMOTION: ["Calm", "Zen", "Zen"]},
    )
    assert sorted(t.name for t in trade.tags) == ["Calm", "Supply/Demand zone", "Zen"]
    set_trade_tags(session, trade, {TagCategory.SETUP: ["Golden Ratio - 0.618 retracement"]})
    assert sorted(t.name for t in trade.tags) == ["Calm", "Golden Ratio - 0.618 retracement", "Zen"]
    zen = session.scalars(select(Tag).where(Tag.name == "Zen")).one()
    assert zen.category is TagCategory.EMOTION


def test_screenshots_are_stored_and_deleted(session: Session, trade: Trade, tmp_path: Path) -> None:
    shot = add_screenshot(session, trade, "chart.PNG", b"\x89PNG data", tmp_path)
    path = Path(shot.path)
    assert path.read_bytes() == b"\x89PNG data"
    assert path.parent == tmp_path / f"trade_{trade.id}"
    assert shot.caption == "chart.PNG"
    delete_screenshot(session, shot)
    assert not path.exists()
    assert session.scalars(select(Screenshot)).all() == []


def test_screenshot_rejects_non_images(session: Session, trade: Trade, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        add_screenshot(session, trade, "evil.exe", b"", tmp_path)


def test_delete_trade_removes_files(session: Session, trade: Trade, tmp_path: Path) -> None:
    shot = add_screenshot(session, trade, "a.jpg", b"x", tmp_path)
    path = Path(shot.path)
    delete_trade(session, trade)
    assert not path.exists()
    assert session.scalars(select(Trade)).all() == []


def test_set_playbook_records_checklist(session: Session, trade: Trade) -> None:
    pb = Playbook(name="ORB")
    pb.checklist_items = [PlaybookChecklistItem(text=t, position=i) for i, t in enumerate("abc")]
    session.add(pb)
    session.flush()
    first, _, third = pb.checklist_items
    set_playbook(session, trade, pb, [first.id, third.id])
    assert trade.playbook_id == pb.id
    assert [r.met for r in sorted(trade.checklist_results, key=lambda r: r.item_id)] == [
        True,
        False,
        True,
    ]
    set_playbook(session, trade, None)
    assert trade.playbook_id is None
    assert trade.checklist_results == []


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(seconds=45), "45s"),
        (timedelta(minutes=7, seconds=5), "7m"),
        (timedelta(hours=1, minutes=5), "1h 05m"),
        (timedelta(days=2, hours=3), "2d 3h"),
        (None, "–"),
    ],
)
def test_format_duration(delta: timedelta | None, text: str) -> None:
    assert format_duration(delta) == text
