"""Helpers shared by all Streamlit pages: DB sessions, global filters, formatting."""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.db import get_engine, init_db, session_scope
from core.instruments import ACCOUNT_CURRENCY, AssetClass
from core.models import Account, Trade
from core.repository import load_trades

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
    asset_classes: tuple[str, ...] = ()
    currency: str = ACCOUNT_CURRENCY

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
        format_func=lambda i: by_id[i].name,
        key="filter_accounts",
    )
    if not selected:
        st.sidebar.caption("No account selected — showing all.")
        selected = list(by_id)

    asset_classes = st.sidebar.multiselect(
        "Markets",
        options=[a.value for a in AssetClass],
        format_func=lambda v: AssetClass(v).label,
        key="filter_assets",
        placeholder="All markets",
    )

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

    st.sidebar.caption(f"All amounts in {ACCOUNT_CURRENCY}.")
    return Filters(
        account_ids=tuple(selected),
        start=start,
        end=end,
        asset_classes=tuple(asset_classes),
    )


def get_filters() -> Filters:
    filters = st.session_state.get("filters")
    if filters is None:
        raise RuntimeError("Filters not initialised; run the app via app/main.py")
    return filters


def fmt_money(
    value: float | None, currency: str = "", signed: bool = True, decimals: int = 2
) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    sign = "+" if signed and value > 0 else "-" if value < 0 else ""
    prefix = f"{currency} " if currency else ""
    return f"{sign}{prefix}{abs(value):,.{decimals}f}"


def fmt_tile_money(value: float | None, signed: bool = True) -> str:
    """Money for narrow stat tiles: whole dollars from 10,000 so the figure fits."""
    big = value is not None and not math.isnan(value) and abs(value) >= 10_000
    return fmt_money(value, signed=signed, decimals=0 if big else 2)


def fmt_price(value: float | None) -> str:
    """Prices with the precision the market needs: 1.16523, 147.512, 431.20, 112,503.50."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    decimals = 5 if abs(value) < 10 else 3 if abs(value) < 1000 else 2
    text = f"{value:,.{decimals}f}"
    if decimals > 2:
        text = text.rstrip("0")
        if len(text.split(".")[1]) < 2:
            text = f"{value:,.2f}"
    return text


def fmt_qty(value: float) -> str:
    """Shares/contracts as integers, lots and coins with their decimals."""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.4f}".rstrip("0")


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


def load_filtered_trades(filters: Filters | None = None) -> pd.DataFrame:
    """All trades (open and closed) matching the sidebar filters."""
    filters = filters or get_filters()
    with db() as s:
        df = load_trades(s, filters.account_ids, filters.start, filters.end)
    if filters.asset_classes:
        df = df[df["asset_class"].isin(filters.asset_classes)]
    return df


def stat_tile(label: str, value: str, help: str | None = None) -> None:
    st.metric(label, value, help=help, border=True)
