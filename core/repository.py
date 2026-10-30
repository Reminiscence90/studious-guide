"""Queries that load trades from the database into DataFrames for the metrics module."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

import pandas as pd
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from core.metrics import add_dimensions
from core.models import Trade, TradeStatus

TRADE_COLUMNS = [
    "id",
    "account_id",
    "account",
    "symbol",
    "instrument",
    "asset_class",
    "point_value",
    "side",
    "status",
    "quantity",
    "avg_entry_price",
    "avg_exit_price",
    "entry_time",
    "exit_time",
    "fees",
    "gross_pnl",
    "net_pnl",
    "stop_loss",
    "playbook_id",
    "playbook",
    "setups",
    "mistakes",
    "emotions",
    "tags",
    "checklist_total",
    "checklist_met",
]


def load_trades(
    session: Session,
    account_ids: Sequence[int] | None = None,
    start: date | None = None,
    end: date | None = None,
    *,
    include_open: bool = True,
) -> pd.DataFrame:
    """Trades as a DataFrame (see ``TRADE_COLUMNS`` plus derived report dimensions).

    Date filters apply to the trade date: the exit date for closed trades and the
    entry date for open ones. Open trades are included unless ``include_open`` is
    False; the metrics functions ignore them either way.
    """
    stmt = select(Trade).options(
        selectinload(Trade.tags),
        selectinload(Trade.account),
        selectinload(Trade.playbook),
        selectinload(Trade.checklist_results),
    )
    if account_ids:
        stmt = stmt.where(Trade.account_id.in_(list(account_ids)))
    if not include_open:
        stmt = stmt.where(Trade.status == TradeStatus.CLOSED)
    if start is not None:
        lo = datetime.combine(start, time.min)
        stmt = stmt.where(
            or_(Trade.exit_time >= lo, (Trade.exit_time.is_(None)) & (Trade.entry_time >= lo))
        )
    if end is not None:
        hi = datetime.combine(end + timedelta(days=1), time.min)
        stmt = stmt.where(
            or_(Trade.exit_time < hi, (Trade.exit_time.is_(None)) & (Trade.entry_time < hi))
        )
    stmt = stmt.order_by(Trade.entry_time)

    rows = []
    for t in session.scalars(stmt):
        by_cat: dict[str, list[str]] = {"setup": [], "mistake": [], "emotion": []}
        for tag in sorted(t.tags, key=lambda x: x.name):
            by_cat[tag.category.value].append(tag.name)
        rows.append(
            {
                "id": t.id,
                "account_id": t.account_id,
                "account": t.account.name,
                "symbol": t.symbol,
                "instrument": t.instrument or t.symbol,
                "asset_class": t.asset_class.value,
                "point_value": t.point_value,
                "side": t.side.value,
                "status": t.status.value,
                "quantity": t.quantity,
                "avg_entry_price": t.avg_entry_price,
                "avg_exit_price": t.avg_exit_price,
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "fees": t.fees,
                "gross_pnl": t.gross_pnl,
                "net_pnl": t.net_pnl,
                "stop_loss": t.stop_loss,
                "playbook_id": t.playbook_id,
                "playbook": t.playbook.name if t.playbook else None,
                "setups": by_cat["setup"],
                "mistakes": by_cat["mistake"],
                "emotions": by_cat["emotion"],
                "tags": [f"{cat}: {n}" for cat in by_cat for n in by_cat[cat]],
                "checklist_total": len(t.checklist_results),
                "checklist_met": sum(r.met for r in t.checklist_results),
            }
        )
    df = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    for col in (
        "quantity",
        "avg_entry_price",
        "avg_exit_price",
        "fees",
        "net_pnl",
        "stop_loss",
        "point_value",
    ):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    return add_dimensions(df)
