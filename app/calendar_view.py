"""Monthly P&L calendar with weekly totals. Click a day to see its trades and journal."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.common import currency_warning, db, fmt_money, get_filters, load_filtered_trades
from core import metrics as m
from core.models import JournalEntry

WEEKDAY_HEADERS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MOODS = {1: "😞", 2: "🙁", 3: "😐", 4: "🙂", 5: "😄"}

filters = get_filters()
cur = filters.currency
df = load_filtered_trades(filters)
closed = m.closed_trades(df)
daily = m.daily_pnl(df)
daily_by_date = daily.set_index("date") if not daily.empty else pd.DataFrame()

st.title("Calendar")
currency_warning(filters)

# ------------------------------------------------------------------ month picker
months = sorted({(d.year, d.month) for d in daily["date"]}) if not daily.empty else []
today = date.today()
if not months:
    months = [(today.year, today.month)]
if st.session_state.get("calendar_month") not in months:
    st.session_state["calendar_month"] = months[-1]


def _shift(step: int) -> None:
    idx = months.index(st.session_state["calendar_month"]) + step
    st.session_state["calendar_month"] = months[max(0, min(idx, len(months) - 1))]


c_prev, c_pick, c_next = st.columns([1, 4, 1], vertical_alignment="bottom")
idx = months.index(st.session_state["calendar_month"])
c_prev.button("◀ Prev", on_click=_shift, args=(-1,), disabled=idx == 0, width="stretch")
c_pick.selectbox(
    "Month",
    months,
    key="calendar_month",
    format_func=lambda ym: date(ym[0], ym[1], 1).strftime("%B %Y"),
    label_visibility="collapsed",
)
c_next.button(
    "Next ▶", on_click=_shift, args=(1,), disabled=idx == len(months) - 1, width="stretch"
)
year, month = st.session_state["calendar_month"]

with db() as s:
    moods = {
        j.day: j.mood
        for j in s.scalars(
            select(JournalEntry).where(
                JournalEntry.day >= date(year, month, 1),
                JournalEntry.day <= date(year + month // 12, month % 12 + 1, 1),
            )
        )
    }

# ------------------------------------------------------------------ month summary
in_month = (
    daily[[d.year == year and d.month == month for d in daily["date"]]]
    if not daily.empty
    else daily
)
s1, s2, s3, s4 = st.columns(4)
s1.metric("Month P&L", fmt_money(float(in_month["net_pnl"].sum()) if len(in_month) else 0, cur))
s2.metric("Trading days", len(in_month))
s3.metric("Green days", int((in_month["net_pnl"] > 0).sum()) if len(in_month) else 0)
s4.metric("Trades", int(in_month["trades"].sum()) if len(in_month) else 0)

# ------------------------------------------------------------------ day dialog


@st.dialog("Day details", width="large")
def show_day(day: date) -> None:
    trades = closed[closed["exit_time"].dt.date == day] if not closed.empty else closed
    pnl = float(trades["net_pnl"].sum()) if len(trades) else 0.0
    st.subheader(f"{day:%A, %d %B %Y}")
    st.write(f"**{fmt_money(pnl, cur)}** across {len(trades)} trades")
    if len(trades):
        table = trades[["id", "symbol", "side", "quantity", "entry_time", "exit_time", "net_pnl",
                        "r_multiple", "setups", "mistakes"]]  # fmt: skip
        event = st.dataframe(
            table,
            hide_index=True,
            width="stretch",
            on_select="rerun",
            selection_mode="single-row",
            column_config={
                "id": None,
                "entry_time": st.column_config.DatetimeColumn("Entry", format="D MMM HH:mm"),
                "exit_time": st.column_config.DatetimeColumn("Exit", format="HH:mm"),
                "net_pnl": st.column_config.NumberColumn("Net P&L", format="%+.2f"),
                "r_multiple": st.column_config.NumberColumn("R", format="%.2f"),
            },
            key=f"day_trades_{day}",
        )
        st.caption("Select a row to open the trade.")
        rows = event.selection.rows if event else []
        if rows:
            st.session_state["selected_trade_id"] = int(table.iloc[rows[0]]["id"])
            st.switch_page("trade_detail.py")

    st.divider()
    st.subheader("Journal")
    with db() as s:
        entry = s.scalars(select(JournalEntry).where(JournalEntry.day == day)).first()
        content = (
            None
            if entry is None
            else (entry.mood, entry.pre_market_plan, entry.post_market_review, entry.notes)
        )
    if content is None:
        st.caption("No journal entry for this day.")
    else:
        mood, plan, review, notes = content
        if mood:
            st.write(f"Mood: {MOODS[mood]} ({mood}/5)")
        if plan:
            st.markdown(f"**Pre-market plan**\n\n{plan}")
        if review:
            st.markdown(f"**Post-market review**\n\n{review}")
        if notes:
            st.markdown(f"**Notes**\n\n{notes}")
    if st.button("✏️ Open in journal"):
        st.session_state["journal_day"] = day
        st.switch_page("journal.py")


# ------------------------------------------------------------------ grid
css = [
    "div[class*='st-key-cal_'] button {min-height: 92px; width: 100%; align-items: flex-start;"
    " justify-content: flex-start; padding: 6px 8px;}",
    "div[class*='st-key-cal_'] button p {white-space: pre-line; text-align: left;"
    " line-height: 1.35; font-size: 0.85rem;}",
    ".cal-week {min-height: 92px; border-radius: 8px; padding: 6px 8px; font-size: 0.85rem;"
    " border: 1px solid rgba(128,128,128,0.3); line-height: 1.35;}",
]
grid = m.month_grid(year, month)
weekly = m.weekly_totals(daily)
weekly_by_start = weekly.set_index("week_start") if not weekly.empty else pd.DataFrame()

for week in grid:
    for d in week:
        if d is None or d not in daily_by_date.index:
            continue
        pnl = float(daily_by_date.loc[d, "net_pnl"])
        bg, border = (
            ("rgba(27,175,122,0.18)", "#1baf7a")
            if pnl > 0
            else ("rgba(227,73,72,0.16)", "#e34948")
            if pnl < 0
            else ("rgba(128,128,128,0.12)", "#888")
        )
        css.append(
            f".st-key-cal_{d:%Y%m%d} button {{background: {bg}; border: 1px solid {border};}}"
        )
st.html(f"<style>{' '.join(css)}</style>")

header = st.columns(8)
for col, name in zip(header, [*WEEKDAY_HEADERS, "Week"], strict=True):
    col.markdown(f"**{name}**")

for week in grid:
    cols = st.columns(8)
    for col, d in zip(cols[:7], week, strict=True):
        with col:
            if d is None:
                st.html("<div style='min-height:92px'></div>")
                continue
            mood = MOODS.get(moods.get(d) or 0, "")
            if d in daily_by_date.index:
                row = daily_by_date.loc[d]
                n = int(row["trades"])
                label = (
                    f"**{d.day}** {mood}\n{fmt_money(float(row['net_pnl']))}\n"
                    f"{n} trade{'s' if n != 1 else ''}"
                )
            else:
                label = f"{d.day} {mood}\n\n"
            if st.button(label, key=f"cal_{d:%Y%m%d}", width="stretch"):
                show_day(d)
    week_start = min(d for d in week if d)
    week_start = date.fromordinal(week_start.toordinal() - week_start.weekday())
    with cols[7]:
        if week_start in weekly_by_start.index:
            w = weekly_by_start.loc[week_start]
            st.html(
                "<div class='cal-week'>"
                f"<b>Week</b><br>{fmt_money(float(w['net_pnl']))}<br>{int(w['trades'])} trades"
                "</div>"
            )
        else:
            st.html("<div class='cal-week'><b>Week</b><br>–</div>")

st.caption(
    "Days are colored by net P&L of trades closed that day (green = profit, red = loss); "
    "the emoji is the journal mood. Weekly totals cover the whole Monday–Sunday week."
)
