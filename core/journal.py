"""Daily journal entries and playbook definitions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import JournalEntry, Playbook, PlaybookChecklistItem


def get_journal_entry(session: Session, day: date) -> JournalEntry | None:
    return session.scalars(select(JournalEntry).where(JournalEntry.day == day)).first()


def save_journal_entry(
    session: Session,
    day: date,
    *,
    pre_market_plan: str = "",
    post_market_review: str = "",
    mood: int | None = None,
    notes: str = "",
) -> JournalEntry:
    """Create or update the single journal entry for ``day``."""
    if mood is not None and not 1 <= mood <= 5:
        raise ValueError("Mood must be between 1 and 5")
    entry = get_journal_entry(session, day)
    if entry is None:
        entry = JournalEntry(day=day)
        session.add(entry)
    entry.pre_market_plan = pre_market_plan
    entry.post_market_review = post_market_review
    entry.mood = mood
    entry.notes = notes
    session.flush()
    return entry


def save_playbook(
    session: Session,
    *,
    name: str,
    description: str = "",
    entry_rules: str = "",
    exit_rules: str = "",
    checklist: Sequence[str] = (),
    playbook_id: int | None = None,
) -> Playbook:
    """Create or update a playbook.

    Checklist items whose text is unchanged keep their identity, so the
    met/not-met history recorded on trades is preserved. Removed items (and their
    recorded results) are deleted.
    """
    name = name.strip()
    if not name:
        raise ValueError("Playbook name is required")
    clash = session.scalars(select(Playbook).where(Playbook.name == name)).first()
    if clash is not None and clash.id != playbook_id:
        raise ValueError(f"A playbook named {name!r} already exists")

    if playbook_id is None:
        playbook = Playbook(name=name)
        session.add(playbook)
    else:
        found = session.get(Playbook, playbook_id)
        if found is None:
            raise ValueError(f"Playbook {playbook_id} not found")
        playbook = found
    playbook.name = name
    playbook.description = description
    playbook.entry_rules = entry_rules
    playbook.exit_rules = exit_rules

    existing = {item.text: item for item in playbook.checklist_items}
    items: list[PlaybookChecklistItem] = []
    for position, text in enumerate(dict.fromkeys(t.strip() for t in checklist if t.strip())):
        item = existing.pop(text, None) or PlaybookChecklistItem(text=text)
        item.position = position
        items.append(item)
    playbook.checklist_items = items  # delete-orphan removes the rest
    session.flush()
    return playbook
