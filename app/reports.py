"""Breakdowns of P&L, win rate and trade count by symbol, time, holding period, side and tag."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.charts import breakdown_bar_figure
from app.common import currency_warning, fmt_money, get_filters, load_filtered_trades
from core import metrics as m

filters = get_filters()
cur = filters.currency
df = load_filtered_trades(filters)
closed = m.closed_trades(df)

st.title("Reports")
st.caption(f"{filters.label} · {len(closed)} closed trades · amounts in {cur}")
currency_warning(filters)

if closed.empty:
    st.info("No closed trades in this range.")
    st.stop()


def show(table: pd.DataFrame, label: str, *, horizontal: bool = False, key: str) -> None:
    """Bar chart of net P&L per group plus the full stats table."""
    if table.empty:
        st.caption("No data for this breakdown.")
        return
    height = max(260, 34 * len(table) + 60) if horizontal else 340
    st.plotly_chart(
        breakdown_bar_figure(table, cur, horizontal=horizontal, height=height),
        width="stretch",
        key=f"chart_{key}",
    )
    st.dataframe(
        table[["group", "trades", "wins", "losses", "win_rate", "net_pnl", "avg_pnl",
               "profit_factor"]],
        hide_index=True,
        width="stretch",
        column_config={
            "group": label,
            "trades": "Trades",
            "wins": "Wins",
            "losses": "Losses",
            "win_rate": st.column_config.NumberColumn("Win rate", format="percent"),
            "net_pnl": st.column_config.NumberColumn("Net P&L", format="%+,.2f"),
            "avg_pnl": st.column_config.NumberColumn("Avg P&L / trade", format="%+,.2f"),
            "profit_factor": st.column_config.NumberColumn("Profit factor", format="%.2f"),
        },
        key=f"table_{key}",
    )  # fmt: skip


tabs = st.tabs(
    ["Symbol", "Day of week", "Hour of day", "Holding time", "Long vs short", "Tags", "Month"]
)

with tabs[0]:
    show(m.breakdown(closed, "symbol", sort_by="net_pnl"), "Symbol", horizontal=True, key="sym")

with tabs[1]:
    show(m.breakdown(closed, "weekday"), "Day of week", key="dow")
    st.caption("By entry day.")

with tabs[2]:
    by_hour = m.breakdown(closed, "hour")
    by_hour["group"] = by_hour["group"].map(lambda h: f"{int(h):02d}:00")
    show(by_hour, "Entry hour", key="hour")
    st.caption("By the hour the position was opened.")

with tabs[3]:
    show(m.breakdown(closed, "holding_bucket"), "Holding time", key="hold")

with tabs[4]:
    sides = m.breakdown(closed, "side")
    sides["group"] = sides["group"].str.title()
    show(sides, "Side", key="side")

with tabs[5]:
    setups = m.breakdown(closed, "setups", explode=True, sort_by="net_pnl")
    mistakes = m.breakdown(closed, "mistakes", explode=True, sort_by="net_pnl", ascending=True)
    emotions = m.breakdown(closed, "emotions", explode=True, sort_by="net_pnl")

    c1, c2 = st.columns(2)
    if not setups.empty:
        best = setups.iloc[0]
        c1.success(
            f"**Best setup:** {best['group']} — {fmt_money(best['net_pnl'], cur)} over "
            f"{best['trades']} trades ({best['win_rate']:.0%} win rate)"
        )
    if not mistakes.empty:
        worst = mistakes.iloc[0]
        c2.error(
            f"**Costliest mistake:** {worst['group']} — {fmt_money(worst['net_pnl'], cur)} over "
            f"{worst['trades']} trades"
        )
        cost = float(mistakes.loc[mistakes["net_pnl"] < 0, "net_pnl"].sum())
        tagged = closed[closed["mistakes"].map(len) > 0]
        st.caption(
            f"Trades tagged with a mistake: {len(tagged)} of {len(closed)}, net "
            f"{fmt_money(m.net_pnl(tagged), cur)}. Losing mistake tags sum to "
            f"{fmt_money(cost, cur)} (a trade with two mistakes counts under both)."
        )

    st.subheader("Setups")
    show(setups, "Setup", horizontal=True, key="setups")
    st.subheader("Mistakes")
    show(mistakes, "Mistake", horizontal=True, key="mistakes")
    st.subheader("Emotions")
    show(emotions, "Emotion", horizontal=True, key="emotions")

with tabs[6]:
    show(m.breakdown(closed, "month"), "Month", key="month")
    st.caption("By exit month.")
