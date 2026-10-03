"""Sortable, filterable table of all trades. Select a row to open the trade."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.common import currency_warning, fmt_money, get_filters, load_filtered_trades
from core import metrics as m

filters = get_filters()
df = load_filtered_trades(filters)

st.title("Trade log")
currency_warning(filters)

if df.empty:
    st.info("No trades in this range. Import trades or widen the date filter.")
    st.stop()

# ------------------------------------------------------------------ filters
with st.container(border=True):
    c1, c2, c3, c4 = st.columns(4)
    symbols = c1.multiselect("Symbol", sorted(df["symbol"].unique()))
    side = c2.segmented_control("Side", ["long", "short"], selection_mode="multi")
    status = c3.segmented_control(
        "Status", ["closed", "open"], selection_mode="multi", default=["closed", "open"]
    )
    result = c4.segmented_control("Result", ["win", "loss", "break-even"], selection_mode="multi")

    c1, c2, c3 = st.columns(3)
    setups = c1.multiselect("Setups", sorted({t for ts in df["setups"] for t in ts}))
    mistakes = c2.multiselect("Mistakes", sorted({t for ts in df["mistakes"] for t in ts}))
    playbooks = c3.multiselect("Playbook", sorted(df["playbook"].dropna().unique()))

mask = pd.Series(True, index=df.index)
if symbols:
    mask &= df["symbol"].isin(symbols)
if side:
    mask &= df["side"].isin(side)
if status:
    mask &= df["status"].isin(status)
if result:
    outcome = pd.Series("break-even", index=df.index)
    outcome[df["net_pnl"] > 0] = "win"
    outcome[df["net_pnl"] < 0] = "loss"
    mask &= outcome.isin(result) & (df["status"] == "closed")
if setups:
    mask &= df["setups"].map(lambda ts: any(t in ts for t in setups))
if mistakes:
    mask &= df["mistakes"].map(lambda ts: any(t in ts for t in mistakes))
if playbooks:
    mask &= df["playbook"].isin(playbooks)
view = df[mask].sort_values("entry_time", ascending=False)

stats = m.summary_stats(view)
st.caption(
    f"{len(view)} trades · net {fmt_money(stats.net_pnl, filters.currency)} · "
    f"win rate {(stats.win_rate or 0):.0%} · click a column header to sort, select a row to open"
)

table = view[
    ["id", "symbol", "side", "status", "entry_time", "exit_time", "quantity", "avg_entry_price",
     "avg_exit_price", "net_pnl", "r_multiple", "holding_minutes", "setups", "mistakes",
     "emotions", "playbook", "account"]
].reset_index(drop=True)  # fmt: skip

event = st.dataframe(
    table,
    hide_index=True,
    width="stretch",
    height=620,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "id": st.column_config.NumberColumn("#", width="small"),
        "symbol": "Symbol",
        "side": "Side",
        "status": "Status",
        "entry_time": st.column_config.DatetimeColumn("Entry", format="YYYY-MM-DD HH:mm"),
        "exit_time": st.column_config.DatetimeColumn("Exit", format="YYYY-MM-DD HH:mm"),
        "quantity": st.column_config.NumberColumn("Qty", format="%.0f"),
        "avg_entry_price": st.column_config.NumberColumn("Entry px", format="%.3f"),
        "avg_exit_price": st.column_config.NumberColumn("Exit px", format="%.3f"),
        "net_pnl": st.column_config.NumberColumn("Net P&L", format="%+.2f"),
        "r_multiple": st.column_config.NumberColumn("R", format="%+.2f"),
        "holding_minutes": st.column_config.NumberColumn("Hold (min)", format="%.0f"),
        "setups": st.column_config.ListColumn("Setups"),
        "mistakes": st.column_config.ListColumn("Mistakes"),
        "emotions": st.column_config.ListColumn("Emotions"),
        "playbook": "Playbook",
        "account": "Account",
    },
    key="trade_log_table",
)

rows = event.selection.rows if event else []
if rows:
    st.session_state["selected_trade_id"] = int(table.iloc[rows[0]]["id"])
    st.switch_page("trade_detail.py")

export = table.assign(
    setups=table["setups"].map(", ".join),
    mistakes=table["mistakes"].map(", ".join),
    emotions=table["emotions"].map(", ".join),
)
st.download_button("⬇️ Download CSV", export.to_csv(index=False), "trades.csv", mime="text/csv")
