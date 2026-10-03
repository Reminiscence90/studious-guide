"""Helpers shared by all Streamlit pages: DB sessions, global filters, formatting."""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta

import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.db import get_engine, init_db, session_scope
from core.models import Account, Trade

GREEN = "#16a34a"
RED = "#dc2626"
NEUTRAL = "#64748b"

DATE_PRESETS = ["All time", "This month", "Last 30 days", "Last 90 days", "Year to date", "Custom"]


@st.cache_resource
def _initialised_engine():  # type: ignore[no-untyped-def]
    engine = get_engine()
    init_db(engine)
    return engine


@contextmanager
def db() -> Iterator[Session]:
    """Transactional session bound to the app's engine."""
    with session_scope(_initialised_engine()) as session:
        yield session


@dataclass(frozen=True)
class Filters:
    account_ids: tuple[int, ...]
    start: date | None
    end: date | None
    currency: str
    mixed_currency: bool

    @property
    def label(self) -> str:
        if self.start is None and self.end is None:
            return "All time"
        return f"{self.start:%d %b %Y} – {self.end:%d %b %Y}"


def accounts() -> list[Account]:
    with db() as s:
        return list(s.scalars(select(Account).order_by(Account.id)))


def _trade_date_bounds() -> tuple[date, date]:
    with db() as s:
        lo, hi = s.execute(select(func.min(Trade.entry_time), func.max(Trade.exit_time))).one()
    today = date.today()
    return (lo.date() if lo else today, hi.date() if hi else today)


def render_sidebar_filters() -> Filters:
    """Account and date-range filters shown on every page. Stored in session state."""
    all_accounts = accounts()
    by_id = {a.id: a for a in all_accounts}
    st.sidebar.header("Filters")
    selected = st.sidebar.multiselect(
        "Accounts",
        options=list(by_id),
        default=list(by_id),
        format_func=lambda i: f"{by_id[i].name} ({by_id[i].currency})",
        key="filter_accounts",
    )
    if not selected:
        st.sidebar.caption("No account selected — showing all.")
        selected = list(by_id)

    lo, hi = _trade_date_bounds()
    preset = st.sidebar.selectbox("Date range", DATE_PRESETS, key="filter_preset")
    start: date | None
    end: date | None
    anchor = hi  # relative ranges are anchored on the latest trade, so demo data stays useful
    match preset:
        case "All time":
            start, end = None, None
        case "This month":
            start, end = anchor.replace(day=1), anchor
        case "Last 30 days":
            start, end = anchor - timedelta(days=29), anchor
        case "Last 90 days":
            start, end = anchor - timedelta(days=89), anchor
        case "Year to date":
            start, end = anchor.replace(month=1, day=1), anchor
        case _:
            picked = st.sidebar.date_input("From / to", value=(lo, hi), key="filter_custom")
            if isinstance(picked, tuple) and len(picked) == 2:
                start, end = picked
            else:
                start, end = lo, hi
    if preset not in ("All time", "Custom"):
        st.sidebar.caption(f"{start:%d %b %Y} – {end:%d %b %Y} (relative to latest trade)")

    currencies = sorted({by_id[i].currency for i in selected if i in by_id})
    return Filters(
        account_ids=tuple(selected),
        start=start,
        end=end,
        currency=currencies[0] if len(currencies) == 1 else "/".join(currencies),
        mixed_currency=len(currencies) > 1,
    )


def get_filters() -> Filters:
    filters = st.session_state.get("filters")
    if filters is None:
        raise RuntimeError("Filters not initialised; run the app via app/main.py")
    return filters


def currency_warning(filters: Filters) -> None:
    if filters.mixed_currency:
        st.warning(
            f"The selected accounts use different currencies ({filters.currency}). "
            "Totals are summed without conversion — filter to one account for exact figures."
        )


def fmt_money(value: float | None, currency: str = "", signed: bool = True) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    sign = "+" if signed and value > 0 else "-" if value < 0 else ""
    prefix = f"{currency} " if currency else ""
    return f"{sign}{prefix}{abs(value):,.2f}"


def fmt_ratio(value: float | None, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    if math.isinf(value):
        return "∞"
    return f"{value:,.{digits}f}"


def fmt_pct(value: float | None) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    return f"{value * 100:.1f}%"


def pnl_color(value: float) -> str:
    return GREEN if value > 0 else RED if value < 0 else NEUTRAL


def open_trade(trade_id: int) -> None:
    """Navigate to the trade detail page for a trade."""
    st.session_state["selected_trade_id"] = trade_id
    st.switch_page("trade_detail.py")
