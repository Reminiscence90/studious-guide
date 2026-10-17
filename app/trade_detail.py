"""Trade detail: prices, P&L, R-multiple, holding time, notes, tags, playbook, screenshots."""

from __future__ import annotations

from pathlib import Path

import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.common import (
    db,
    fmt_money,
    fmt_price,
    fmt_qty,
    get_filters,
    load_filtered_trades,
    pnl_color,
)
from core import metrics as m
from core.db import SCREENSHOT_DIR
from core.instruments import ACCOUNT_CURRENCY, AssetClass
from core.models import Playbook, Screenshot, Tag, TagCategory, Trade, TradeStatus
from core.trades import (
    add_screenshot,
    delete_screenshot,
    delete_trade,
    format_duration,
    set_playbook,
    set_trade_tags,
)

QTY_HELP = {
    AssetClass.STOCK: "Shares",
    AssetClass.FUTURE: "Contracts",
    AssetClass.FOREX: "Lots",
    AssetClass.COMMODITY: "Contracts (futures) or lots (spot)",
    AssetClass.CRYPTO: "Coins",
}

filters = get_filters()
all_trades = load_filtered_trades(filters).sort_values("entry_time", ascending=False)

st.title("Trade detail")
if all_trades.empty:
    st.info("No trades in this range.")
    st.stop()

ids = all_trades["id"].tolist()
labels = {
    row.id: f"#{row.id} · {row.entry_time:%Y-%m-%d %H:%M} · {row.symbol} {row.side} · "
    + ("open" if row.status == "open" else fmt_money(row.net_pnl))
    for row in all_trades.itertuples()
}
selected = st.session_state.get("selected_trade_id")
if selected not in labels:
    selected = ids[0]
st.session_state["trade_picker"] = selected


def _pick() -> None:
    st.session_state["selected_trade_id"] = st.session_state["trade_picker"]


def _step(delta: int) -> None:
    idx = ids.index(st.session_state["selected_trade_id"]) + delta
    st.session_state["selected_trade_id"] = ids[max(0, min(idx, len(ids) - 1))]


st.session_state["selected_trade_id"] = selected
c_prev, c_pick, c_next = st.columns([1, 6, 1], vertical_alignment="bottom")
pos = ids.index(selected)
c_prev.button("◀ Newer", on_click=_step, args=(-1,), disabled=pos == 0, width="stretch")
c_pick.selectbox(
    "Trade", ids, format_func=labels.get, key="trade_picker", on_change=_pick,
    label_visibility="collapsed",
)  # fmt: skip
c_next.button("Older ▶", on_click=_step, args=(1,), disabled=pos == len(ids) - 1, width="stretch")

trade_id = int(selected)

with db() as s:
    trade = s.scalars(
        select(Trade)
        .where(Trade.id == trade_id)
        .options(
            selectinload(Trade.executions),
            selectinload(Trade.tags),
            selectinload(Trade.screenshots),
            selectinload(Trade.account),
            selectinload(Trade.playbook),
            selectinload(Trade.checklist_results),
        )
    ).one()
    all_tags = list(s.scalars(select(Tag).order_by(Tag.name)))
    playbooks = list(s.scalars(select(Playbook).options(selectinload(Playbook.checklist_items))))
    executions = [
        (e.timestamp, e.raw_symbol or e.symbol, e.side.value, e.quantity, e.price, e.fees, e.source)
        for e in trade.executions
    ]

cur = ACCOUNT_CURRENCY
is_open = trade.status is TradeStatus.OPEN
holding = (trade.exit_time - trade.entry_time) if trade.exit_time else None
r_mult = m.r_multiple(
    trade.net_pnl, trade.avg_entry_price, trade.stop_loss, trade.quantity, trade.point_value
)

# ------------------------------------------------------------------ header
color = pnl_color(trade.net_pnl)
st.html(
    f"<div style='display:flex;gap:16px;align-items:baseline;flex-wrap:wrap'>"
    f"<span style='font-size:2rem;font-weight:700'>{trade.symbol}</span>"
    f"<span style='padding:2px 10px;border-radius:12px;border:1px solid {color}'>"
    f"{trade.side.value.upper()}</span>"
    f"<span style='padding:2px 10px;border-radius:12px;border:1px solid rgba(128,128,128,.5)'>"
    f"{trade.asset_class.label}"
    + (f" · {trade.instrument} ×{trade.point_value:,.6g}" if trade.point_value != 1 else "")
    + "</span>"
    f"<span style='opacity:.7'>{trade.status.value} · {trade.account.name}</span>"
    f"<span style='font-size:1.6rem;font-weight:600;color:{color}'>"
    f"{fmt_money(trade.net_pnl, cur)}</span>"
    + ("<span style='opacity:.7'>(realised so far)</span>" if is_open else "")
    + "</div>"
)

c = st.columns(4)
c[0].metric("Quantity", fmt_qty(trade.quantity), help=QTY_HELP[trade.asset_class])
c[1].metric("Avg entry", fmt_price(trade.avg_entry_price))
c[2].metric("Avg exit", fmt_price(trade.avg_exit_price))
c[3].metric("R-multiple", "–" if r_mult is None else f"{r_mult:+.2f}R",
            help="Net P&L ÷ (|entry − stop| × quantity). Set a stop loss to enable.")  # fmt: skip
c = st.columns(4)
c[0].metric("Entry time", f"{trade.entry_time:%d %b %y %H:%M}")
c[1].metric("Exit time", "–" if trade.exit_time is None else f"{trade.exit_time:%d %b %y %H:%M}")
c[2].metric("Holding time", format_duration(holding))
other = trade.net_pnl - (trade.gross_pnl - trade.fees)
c[3].metric(
    "Gross P&L / fees" + (" / swap" if abs(other) >= 0.005 else ""),
    f"{trade.gross_pnl:,.2f} / {trade.fees:,.2f}"
    + (f" / {other:+,.2f}" if abs(other) >= 0.005 else ""),
    help="Net P&L = gross − fees" + (" + swap/financing (from the broker's net P&L)"
                                     if abs(other) >= 0.005 else ""),
)  # fmt: skip

left, right = st.columns([3, 2], gap="large")

# ------------------------------------------------------------------ notes
with left:
    st.subheader("Notes")
    tab_view, tab_edit = st.tabs(["Preview", "Edit"])
    with tab_edit:
        notes = st.text_area(
            "Markdown notes", value=trade.notes, height=200, key=f"notes_{trade_id}"
        )
        if st.button("Save notes", key=f"save_notes_{trade_id}"):
            with db() as s:
                obj = s.get(Trade, trade_id)
                assert obj is not None
                obj.notes = notes
            st.toast("Notes saved")
            st.rerun()
    with tab_view:
        st.markdown(trade.notes or "_No notes yet — use the Edit tab._")

    # -------------------------------------------------------------- screenshots
    st.subheader("Screenshots")
    for shot in trade.screenshots:
        if Path(shot.path).exists():
            st.image(shot.path, caption=shot.caption)
        else:
            st.warning(f"Missing file: {shot.path}")
        if st.button("Delete screenshot", key=f"del_shot_{shot.id}"):
            with db() as s:
                obj_shot = s.get(Screenshot, shot.id)
                if obj_shot is not None:
                    delete_screenshot(s, obj_shot)
            st.rerun()
    uploads = st.file_uploader(
        "Add chart screenshots",
        type=["png", "jpg", "jpeg", "gif", "webp"],
        accept_multiple_files=True,
        key=f"upload_{trade_id}_{len(trade.screenshots)}",
    )
    if uploads and st.button("Upload", key=f"do_upload_{trade_id}"):
        with db() as s:
            obj = s.get(Trade, trade_id)
            assert obj is not None
            for f in uploads:
                add_screenshot(s, obj, f.name, f.getvalue(), SCREENSHOT_DIR)
        st.rerun()

    st.subheader("Executions")
    st.dataframe(
        {
            "Time": [e[0] for e in executions],
            "Symbol": [e[1] for e in executions],
            "Side": [e[2] for e in executions],
            "Qty": [e[3] for e in executions],
            "Price": [e[4] for e in executions],
            "Fees": [e[5] for e in executions],
            "Source": [e[6] for e in executions],
        },
        hide_index=True,
        width="stretch",
    )

# ------------------------------------------------------------------ tags / risk / playbook
with right:
    st.subheader("Tags")
    current = {cat: [t.name for t in trade.tags if t.category is cat] for cat in TagCategory}
    chosen: dict[TagCategory, list[str]] = {}
    for cat in TagCategory:
        options = sorted({t.name for t in all_tags if t.category is cat} | set(current[cat]))
        chosen[cat] = st.multiselect(
            cat.value.title() + "s",
            options,
            default=current[cat],
            accept_new_options=True,
            key=f"tags_{cat.value}_{trade_id}",
            help="Type a new name and press Enter to create a custom tag.",
        )
    if st.button("Save tags", key=f"save_tags_{trade_id}"):
        with db() as s:
            obj = s.get(Trade, trade_id)
            assert obj is not None
            set_trade_tags(s, obj, chosen)
        st.toast("Tags saved")
        st.rerun()

    st.subheader("Risk")
    with st.form(f"risk_{trade_id}"):
        stop = st.number_input(
            "Stop loss (0 = none)", min_value=0.0, value=float(trade.stop_loss or 0.0),
            format="%.4f",
        )  # fmt: skip
        if st.form_submit_button("Save stop loss"):
            with db() as s:
                obj = s.get(Trade, trade_id)
                assert obj is not None
                obj.stop_loss = stop or None
            st.rerun()
    if trade.stop_loss:
        risk = abs(trade.avg_entry_price - trade.stop_loss) * trade.quantity * trade.point_value
        st.caption(f"Initial risk: {fmt_money(risk, cur, signed=False)}")

    st.subheader("Playbook")
    pb_by_id = {p.id: p for p in playbooks}
    pb_choice = st.selectbox(
        "Strategy",
        [None, *pb_by_id],
        index=([None, *pb_by_id].index(trade.playbook_id) if trade.playbook_id in pb_by_id else 0),
        format_func=lambda i: "— none —" if i is None else pb_by_id[i].name,
        key=f"pb_{trade_id}",
    )
    met_ids: list[int] = []
    if pb_choice is not None:
        met_before = {r.item_id for r in trade.checklist_results if r.met}
        st.caption("Checklist — tick the criteria this trade met:")
        for item in pb_by_id[pb_choice].checklist_items:
            if st.checkbox(
                item.text,
                value=item.id in met_before and pb_choice == trade.playbook_id,
                key=f"chk_{trade_id}_{pb_choice}_{item.id}",
            ):
                met_ids.append(item.id)
    if st.button("Save playbook", key=f"save_pb_{trade_id}"):
        with db() as s:
            obj = s.get(Trade, trade_id)
            assert obj is not None
            set_playbook(s, obj, s.get(Playbook, pb_choice) if pb_choice else None, met_ids)
        st.toast("Playbook saved")
        st.rerun()

    st.divider()
    with st.popover("🗑️ Delete trade"):
        st.write("This permanently deletes the trade, its executions and screenshots.")
        if st.button("Confirm delete", type="primary", key=f"del_trade_{trade_id}"):
            with db() as s:
                obj = s.get(Trade, trade_id)
                if obj is not None:
                    delete_trade(s, obj)
            st.session_state.pop("selected_trade_id", None)
            st.rerun()
