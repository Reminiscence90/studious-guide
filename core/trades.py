"""Editing operations on trades: tags, screenshots, playbook checklist, deletion."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import (
    Playbook,
    Screenshot,
    Tag,
    TagCategory,
    Trade,
    TradeChecklistResult,
)

ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def get_or_create_tag(session: Session, name: str, category: TagCategory) -> Tag:
    name = name.strip()
    if not name:
        raise ValueError("Tag name is empty")
    tag = session.scalars(select(Tag).where(Tag.name == name, Tag.category == category)).first()
    if tag is None:
        tag = Tag(name=name, category=category)
        session.add(tag)
        session.flush()
    return tag


def set_trade_tags(
    session: Session, trade: Trade, names_by_category: Mapping[TagCategory, Iterable[str]]
) -> None:
    """Replace the trade's tags in the given categories (others are left untouched).

    Unknown names are created as new custom tags.
    """
    categories = set(names_by_category)
    kept = [t for t in trade.tags if t.category not in categories]
    new: list[Tag] = []
    for category, names in names_by_category.items():
        for name in dict.fromkeys(n.strip() for n in names if n.strip()):
            new.append(get_or_create_tag(session, name, category))
    trade.tags = kept + new
    session.flush()


def screenshot_path(base_dir: Path, trade_id: int, filename: str) -> Path:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise ValueError(f"Unsupported image type {suffix!r}")
    return base_dir / f"trade_{trade_id}" / f"{uuid.uuid4().hex}{suffix}"


def add_screenshot(
    session: Session,
    trade: Trade,
    filename: str,
    data: bytes,
    base_dir: Path,
    caption: str = "",
) -> Screenshot:
    """Store an uploaded image on disk under ``base_dir`` and record it on the trade."""
    path = screenshot_path(base_dir, trade.id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    shot = Screenshot(trade_id=trade.id, path=str(path), caption=caption or filename)
    session.add(shot)
    session.flush()
    return shot


def delete_screenshot(session: Session, screenshot: Screenshot) -> None:
    Path(screenshot.path).unlink(missing_ok=True)
    session.delete(screenshot)
    session.flush()


def delete_trade(session: Session, trade: Trade) -> None:
    """Delete a trade with its executions and screenshot files."""
    for shot in list(trade.screenshots):
        Path(shot.path).unlink(missing_ok=True)
    session.delete(trade)
    session.flush()


def set_playbook(
    session: Session, trade: Trade, playbook: Playbook | None, met_item_ids: Iterable[int] = ()
) -> None:
    """Link a trade to a playbook and record which checklist items were met."""
    trade.playbook = playbook
    met = set(met_item_ids)
    trade.checklist_results = (
        []
        if playbook is None
        else [
            TradeChecklistResult(item_id=item.id, met=item.id in met)
            for item in playbook.checklist_items
        ]
    )
    session.flush()


def format_duration(delta: timedelta | None) -> str:
    """Human-friendly holding time, e.g. ``"2d 3h"``, ``"1h 05m"``, ``"45s"``."""
    if delta is None:
        return "–"
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"
