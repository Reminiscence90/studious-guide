"""Accounts, instruments, tags and broker presets."""

from __future__ import annotations

from dataclasses import asdict

import pandas as pd
import streamlit as st
from sqlalchemy import func, select

from app.common import db, delete_record
from core.importer import load_specs, recalculate_trades, replace_instruments
from core.instruments import ACCOUNT_CURRENCY, AssetClass, InstrumentSpec
from core.models import Account, BrokerPreset, Tag, TagCategory, Trade, trade_tags
from core.trades import get_or_create_tag

st.title("Settings")

# ------------------------------------------------------------------ accounts
st.header("Accounts")
st.caption(f"All accounts are in {ACCOUNT_CURRENCY}.")
with db() as s:
    rows = s.execute(
        select(Account, func.count(Trade.id))
        .outerjoin(Trade)
        .group_by(Account.id)
        .order_by(Account.id)
    ).all()
    account_rows = [(a.id, a.name, n) for a, n in rows]

for acc_id, name, n_trades in account_rows:
    with st.expander(f"{name} · {n_trades} trades"):
        with st.form(f"acc_{acc_id}"):
            new_name = st.text_input("Name", value=name)
            if st.form_submit_button("Save"):
                with db() as s:
                    acc = s.get(Account, acc_id)
                    assert acc is not None
                    acc.name = new_name.strip() or name
                st.rerun()
        if len(account_rows) > 1 and st.button(
            "Delete account and its trades", key=f"del_acc_{acc_id}"
        ):
            delete_record(Account, acc_id)

with st.form("new_account", clear_on_submit=True):
    st.subheader("Add account")
    name = st.text_input("Name", key="new_account_name")
    submitted = st.form_submit_button("Add account")
if submitted:
    new_name = name.strip()
    if not new_name:
        st.error("Enter a name for the account.")
    else:
        with db() as s:
            exists = s.scalars(select(Account).where(Account.name == new_name)).first()
            if exists is None:
                s.add(Account(name=new_name))
        # Rerun only after the block above has committed: st.rerun() stops the script
        # at once, so calling it inside the block would discard the new account.
        if exists is None:
            st.rerun()
        st.error(f"An account named {new_name!r} already exists.")

# ------------------------------------------------------------------ instruments
st.header("Instruments")
st.caption(
    "P&L = price move × quantity × multiplier. Futures: quantity in contracts, multiplier = "
    "$ per point (ES 50, NQ 20, CL 1,000). Forex: quantity in lots, multiplier 100,000 "
    "(set 1 if your broker exports units); USD-base pairs such as USDJPY are converted to "
    "USD at the exit price. Futures contract codes (ESZ6, MNQH27) match their root. "
    "Broker-suffixed symbols are treated as their base (XAUUSD.R → XAUUSD, a commodity; "
    "EURUSD.R → EURUSD, forex). Index CFDs (US500, US100, US30) are their own market; "
    "index futures such as ES and NQ stay under Futures. Check the units per lot for index "
    "CFDs: they differ by broker (Alchemy Markets US500: 100). Symbols "
    "not listed are treated as US stocks with multiplier 1."
)
with db() as s:
    current = sorted(load_specs(s).values(), key=lambda x: (x.asset_class.value, x.symbol))
instruments = pd.DataFrame(
    [{**asdict(x), "asset_class": x.asset_class.value} for x in current],
    columns=["symbol", "name", "asset_class", "multiplier", "quote_currency"],
)
edited = st.data_editor(
    instruments,
    num_rows="dynamic",
    hide_index=True,
    width="stretch",
    column_config={
        "symbol": st.column_config.TextColumn("Symbol / futures root", required=True),
        "name": "Name",
        "asset_class": st.column_config.SelectboxColumn(
            "Market", options=[a.value for a in AssetClass], required=True
        ),
        "multiplier": st.column_config.NumberColumn(
            "Multiplier", min_value=0.0, format="%g", required=True
        ),
        "quote_currency": st.column_config.TextColumn("Quote ccy", max_chars=4),
    },
    key="instrument_editor",
)
if st.button("💾 Save instruments and recalculate P&L"):
    specs = [
        InstrumentSpec(
            symbol=str(r.symbol),
            asset_class=AssetClass(r.asset_class),
            multiplier=float(r.multiplier),
            quote_currency=str(r.quote_currency or "USD"),
            name=str(r.name or ""),
        )
        for r in edited.dropna(subset=["symbol", "asset_class", "multiplier"]).itertuples()
    ]
    try:
        with db() as s:
            replace_instruments(s, specs)
            updated = recalculate_trades(s)
    except ValueError as exc:
        st.error(str(exc))
    else:
        st.success(f"Saved {len(specs)} instruments and recalculated {updated} trades.")

# ------------------------------------------------------------------ tags
st.header("Tags")
st.caption("Setups, mistakes and emotions. Add your own; tags are assigned on the trade page.")
with db() as s:
    usage = dict(
        s.execute(select(trade_tags.c.tag_id, func.count()).group_by(trade_tags.c.tag_id)).all()
    )
    tags = [(t.id, t.name, t.category) for t in s.scalars(select(Tag).order_by(Tag.name))]

tag_cols = st.columns(3)
for col, category in zip(tag_cols, TagCategory, strict=True):
    with col:
        st.subheader(category.value.title() + "s")
        for tag_id, tag_name, cat in tags:
            if cat is not category:
                continue
            c1, c2 = st.columns([4, 1])
            c1.write(f"{tag_name} · {usage.get(tag_id, 0)} trades")
            if c2.button("✕", key=f"del_tag_{tag_id}", help="Delete tag"):
                delete_record(Tag, tag_id)
        with st.form(f"new_tag_{category.value}", clear_on_submit=True):
            new_tag = st.text_input("New tag", key=f"new_tag_input_{category.value}")
            if st.form_submit_button("Add") and new_tag.strip():
                with db() as s:
                    get_or_create_tag(s, new_tag, category)
                st.rerun()

# ------------------------------------------------------------------ presets
st.header("Broker presets")
with db() as s:
    presets = [
        (p.id, p.name, dict(p.mapping), p.datetime_format)
        for p in s.scalars(select(BrokerPreset).order_by(BrokerPreset.name))
    ]
if not presets:
    st.caption("No presets yet — save one from the import screen.")
for preset_id, preset_name, mapping, fmt in presets:
    with st.expander(preset_name):
        st.json(mapping)
        st.caption(f"Date format: {fmt or 'auto-detect'}")
        if st.button("Delete preset", key=f"del_preset_{preset_id}"):
            delete_record(BrokerPreset, preset_id)
