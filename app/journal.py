"""Daily journal: pre-market plan, post-market review, mood and notes — one entry per day."""

from __future__ import annotations

from datetime import date

import streamlit as st
from sqlalchemy import select

from app.common import db, fmt_money, get_filters, load_filtered_trades
from core import metrics as m
from core.journal import get_journal_entry, save_journal_entry
from core.models import JournalEntry

MOODS = {1: "😞 Awful", 2: "🙁 Bad", 3: "😐 Neutral", 4: "🙂 Good", 5: "😄 Great"}

filters = get_filters()
df = load_filtered_trades(filters)
closed = m.closed_trades(df)
daily = m.daily_pnl(df)

st.title("Daily journal")

default_day = st.session_state.pop("journal_day", None)
if default_day is None:
    default_day = daily["date"].iloc[-1] if not daily.empty else date.today()
day = st.date_input("Trading day", value=default_day, format="YYYY-MM-DD")

day_trades = closed[closed["exit_time"].dt.date == day] if not closed.empty else closed
pnl = float(day_trades["net_pnl"].sum()) if len(day_trades) else 0.0
wins = int((day_trades["net_pnl"] > 0).sum()) if len(day_trades) else 0

c1, c2, c3 = st.columns(3)
c1.metric("Day P&L", fmt_money(pnl, filters.currency))
c2.metric("Trades", len(day_trades))
c3.metric("Winners", wins)
if len(day_trades):
    with st.expander("Trades closed this day", expanded=False):
        st.dataframe(
            day_trades[
                ["symbol", "side", "quantity", "net_pnl", "r_multiple", "setups", "mistakes"]
            ],
            hide_index=True,
            width="stretch",
            column_config={
                "net_pnl": st.column_config.NumberColumn("Net P&L", format="%+.2f"),
                "r_multiple": st.column_config.NumberColumn("R", format="%+.2f"),
            },
        )

with db() as s:
    entry = get_journal_entry(s, day)
    values = (
        ("", "", None, "")
        if entry is None
        else (entry.pre_market_plan, entry.post_market_review, entry.mood, entry.notes)
    )

plan, review, mood, notes = values
with st.form(f"journal_{day}"):
    new_plan = st.text_area(
        "Pre-market plan",
        value=plan,
        height=120,
        placeholder="Market context, watchlist, levels, max loss…",
    )
    new_review = st.text_area(
        "Post-market review",
        value=review,
        height=120,
        placeholder="What went well? What would you do differently?",
    )
    new_mood = st.radio(
        "Mood",
        [None, *MOODS],
        index=[None, *MOODS].index(mood),
        format_func=lambda v: "Not set" if v is None else MOODS[v],
        horizontal=True,
    )
    new_notes = st.text_area("Notes (markdown)", value=notes, height=100)
    if st.form_submit_button("Save entry", type="primary"):
        with db() as s:
            save_journal_entry(
                s,
                day,
                pre_market_plan=new_plan,
                post_market_review=new_review,
                mood=new_mood,
                notes=new_notes,
            )
        st.toast(f"Journal saved for {day:%d %b %Y}")
        st.rerun()

st.subheader("Recent entries")
with db() as s:
    recent = [
        (e.day, e.mood, e.post_market_review or e.pre_market_plan)
        for e in s.scalars(select(JournalEntry).order_by(JournalEntry.day.desc()).limit(30))
    ]
pnl_by_day = dict(zip(daily["date"], daily["net_pnl"], strict=True)) if not daily.empty else {}
if not recent:
    st.caption("No entries yet.")
else:
    st.dataframe(
        {
            "Day": [d for d, _, _ in recent],
            "Mood": [MOODS.get(mo, "") if mo else "" for _, mo, _ in recent],
            "Day P&L": [pnl_by_day.get(d) for d, _, _ in recent],
            "Summary": [text[:120] for _, _, text in recent],
        },
        hide_index=True,
        width="stretch",
        column_config={"Day P&L": st.column_config.NumberColumn(format="%+.2f")},
    )
