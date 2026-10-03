"""Import trades from CSV (with column mapping and broker presets) or enter them manually."""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.common import accounts, db
from core.importer import (
    FIELD_HELP,
    FIELD_LABELS,
    ORDER_HISTORY_FIELDS,
    REQUIRED_FIELDS,
    ImportError_,
    add_manual_trade,
    detect_preset,
    guess_mapping,
    import_dataframe,
    rows_to_executions,
    save_preset,
    validate_mapping,
)
from core.models import BrokerPreset, TradeSide

NONE = "— not mapped —"

st.title("Import trades")

all_accounts = accounts()
account_by_id = {a.id: a for a in all_accounts}
account_id = st.selectbox(
    "Import into account",
    options=list(account_by_id),
    format_func=lambda i: account_by_id[i].name,
)

csv_tab, manual_tab = st.tabs(["📄 CSV import", "✍️ Manual entry"])

with csv_tab:
    uploaded = st.file_uploader("Broker CSV export", type=["csv"])
    if uploaded is None:
        st.info(
            "Upload a broker export. Three layouts work: one row per **order** (order "
            "history, e.g. Alchemy Markets — detected automatically), one row per "
            "**fill**, or one row per **round trip**. Cancelled orders are ignored, partial "
            "fills are grouped into trades and rows that were already imported are skipped. "
            "Try `sample_data/sample_trades.csv`."
        )
    else:
        try:
            df = pd.read_csv(uploaded, dtype=str, keep_default_na=False)
        except Exception as exc:
            st.error(f"Could not read CSV: {exc}")
            st.stop()

        with db() as s:
            presets = {p.name: (dict(p.mapping), p.datetime_format) for p in s.scalars(
                select(BrokerPreset).order_by(BrokerPreset.name))}  # fmt: skip
        detected = detect_preset(df.columns, {n: mp for n, (mp, _) in presets.items()})
        choices = ["Auto-detect columns", *presets]
        preset_name = st.selectbox(
            "Broker format",
            choices,
            index=choices.index(detected) if detected else 0,
            key=f"preset_{uploaded.name}",
        )
        if detected and preset_name == detected:
            st.success(f"Detected **{detected}** — columns are mapped for you.")
        if preset_name == "Auto-detect columns":
            initial, initial_fmt = guess_mapping(df.columns), None
        else:
            initial, initial_fmt = presets[preset_name]

        with st.expander(f"Preview: {len(df)} rows, {len(df.columns)} columns"):
            st.dataframe(df.head(20), hide_index=True, width="stretch")

        options = [NONE, *df.columns]
        mapping: dict[str, str | None] = {}

        def mapping_inputs(fields: tuple[str, ...], ncols: int = 4) -> None:
            cols = st.columns(ncols)
            for i, fld in enumerate(fields):
                default = initial.get(fld)
                index = options.index(default) if default in options else 0
                label = FIELD_LABELS[fld] + (" *" if fld in REQUIRED_FIELDS else "")
                choice = cols[i % ncols].selectbox(
                    label,
                    options,
                    index=index,
                    help=FIELD_HELP.get(fld),
                    key=f"map_{preset_name}_{fld}",
                )
                mapping[fld] = None if choice == NONE else choice

        with st.expander("Column mapping", expanded=not detected):
            st.markdown("**Every format**")
            mapping_inputs((*REQUIRED_FIELDS, "fees"), ncols=3)
            st.markdown("**Order-history exports** (one row per order)")
            st.caption(
                "Map these when the export lists orders: only filled orders are imported, "
                "fills are grouped by position ID, the broker's P&L is used and the stop "
                "loss is taken from the stop-loss order."
            )
            mapping_inputs(ORDER_HISTORY_FIELDS, ncols=4)
            st.markdown("**Round-trip rows** (entry and exit on one row)")
            mapping_inputs(("exit_price", "exit_time"), ncols=2)
            dt_format = st.text_input(
                "Date/time format (optional, strftime syntax, e.g. `%d/%m/%Y %H:%M`)",
                value=initial_fmt or "",
                help="Leave blank to auto-detect.",
            )

        problems = validate_mapping(mapping, df.columns)
        for p in problems:
            st.warning(p)

        if not problems:
            try:
                parsed = rows_to_executions(df, mapping, dt_format.strip() or None)
            except ImportError_ as exc:
                st.error(str(exc))
                problems = [str(exc)]
            else:
                positions = {(f.symbol, f.position_id) for f in parsed.fills if f.position_id}
                parts = [f"**{len(parsed.fills)} fills** from {len(df)} rows"]
                if parsed.skipped:
                    parts.append(f"{parsed.skipped} cancelled / unfilled orders ignored")
                if positions:
                    parts.append(f"{len(positions)} positions")
                if parsed.errors:
                    parts.append(f"{len(parsed.errors)} rows with errors")
                st.info("Ready to import: " + " · ".join(parts))

        save_col, import_col = st.columns(2)
        with save_col:
            new_preset = st.text_input(
                "Save mapping as preset", placeholder="e.g. Interactive Brokers"
            )
            if st.button("💾 Save preset", disabled=bool(problems) or not new_preset.strip()):
                with db() as s:
                    save_preset(s, new_preset.strip(), mapping, dt_format.strip() or None)
                st.success(f"Preset '{new_preset.strip()}' saved.")
        with import_col:
            st.write("")
            st.write("")
            if st.button("⬆️ Import trades", type="primary", disabled=bool(problems)):
                try:
                    with db() as s:
                        result = import_dataframe(
                            s, account_id, df, mapping, dt_format.strip() or None
                        )
                except ImportError_ as exc:
                    st.error(str(exc))
                else:
                    st.success(
                        f"Imported {result.executions_imported} fills → "
                        f"{result.trades_created} new trades, {result.trades_updated} open "
                        f"trades updated. Skipped {result.duplicates_skipped} already-imported "
                        f"fills and {result.rows_skipped} cancelled / unfilled orders."
                    )
                    if result.warnings:
                        with st.expander(f"ℹ️ {len(result.warnings)} positions not imported"):
                            st.write("\n".join(f"- {w}" for w in result.warnings))
                    if result.errors:
                        with st.expander(f"⚠️ {len(result.errors)} rows could not be imported"):
                            st.write("\n".join(f"- {e}" for e in result.errors))

with manual_tab:
    with st.form("manual_trade", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        symbol = c1.text_input(
            "Symbol *",
            placeholder="TSLA, ESZ6, EURUSD, XAUUSD, BTCUSD…",
            help="Futures contract codes resolve to their root (ESZ6 → ES, $50/point). "
            "Multipliers are set under Settings → Instruments.",
        )
        side = c2.radio("Side", [TradeSide.LONG, TradeSide.SHORT], horizontal=True,
                        format_func=lambda s: s.value.title())  # fmt: skip
        quantity = c3.number_input(
            "Quantity *",
            min_value=0.0,
            step=1.0,
            value=100.0,
            format="%g",
            help="Shares (stocks), contracts (futures), lots (forex / spot commodities) "
            "or coins (crypto).",
        )

        c1, c2, c3 = st.columns(3)
        entry_price = c1.number_input("Entry price *", min_value=0.0, format="%.5f")
        entry_date = c2.date_input("Entry date")
        entry_tm = c3.time_input("Entry time", value=time(9, 30), step=60)

        closed = st.checkbox("Position is closed", value=True)
        c1, c2, c3 = st.columns(3)
        exit_price = c1.number_input("Exit price", min_value=0.0, format="%.5f")
        exit_date = c2.date_input("Exit date")
        exit_tm = c3.time_input("Exit time", value=time(10, 0), step=60)

        c1, c2 = st.columns(2)
        fees = c1.number_input("Fees", min_value=0.0, format="%.2f")
        stop = c2.number_input("Stop loss (optional, 0 = none)", min_value=0.0, format="%.5f")
        notes = st.text_area("Notes (markdown)")
        submitted = st.form_submit_button("Add trade", type="primary")

    if submitted:
        try:
            with db() as s:
                trade = add_manual_trade(
                    s,
                    account_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    entry_price=entry_price,
                    entry_time=datetime.combine(entry_date, entry_tm),
                    exit_price=exit_price if closed else None,
                    exit_time=datetime.combine(exit_date, exit_tm) if closed else None,
                    fees=fees,
                    stop_loss=stop or None,
                    notes=notes,
                )
                summary = None if trade is None else (trade.symbol, trade.status, trade.net_pnl)
        except ImportError_ as exc:
            st.error(str(exc))
        else:
            if summary is None:
                st.warning("An identical trade already exists — nothing added.")
            else:
                sym, status, pnl = summary
                st.success(f"Added {sym} ({status.value}), net P&L {pnl:,.2f}.")
