"""Accounts, tags and broker presets."""

from __future__ import annotations

import streamlit as st
from sqlalchemy import func, select

from app.common import db
from core.models import Account, BrokerPreset, Tag, TagCategory, Trade, trade_tags

CURRENCIES = ["MYR", "USD", "SGD", "EUR", "GBP", "AUD", "HKD", "JPY", "IDR", "THB"]

st.title("Settings")

# ------------------------------------------------------------------ accounts
st.header("Accounts")
with db() as s:
    rows = s.execute(
        select(Account, func.count(Trade.id))
        .outerjoin(Trade)
        .group_by(Account.id)
        .order_by(Account.id)
    ).all()
    account_rows = [(a.id, a.name, a.currency, n) for a, n in rows]

for acc_id, name, currency, n_trades in account_rows:
    with st.expander(f"{name} — {currency} · {n_trades} trades"):
        with st.form(f"acc_{acc_id}"):
            new_name = st.text_input("Name", value=name)
            idx = CURRENCIES.index(currency) if currency in CURRENCIES else 0
            new_currency = st.selectbox("Currency", CURRENCIES, index=idx)
            if st.form_submit_button("Save"):
                with db() as s:
                    acc = s.get(Account, acc_id)
                    assert acc is not None
                    acc.name, acc.currency = new_name.strip() or name, new_currency
                st.rerun()
        if len(account_rows) > 1 and st.button(
            "Delete account and its trades", key=f"del_acc_{acc_id}"
        ):
            with db() as s:
                acc = s.get(Account, acc_id)
                if acc is not None:
                    s.delete(acc)
            st.rerun()

with st.form("new_account", clear_on_submit=True):
    st.subheader("Add account")
    c1, c2 = st.columns(2)
    name = c1.text_input("Name")
    currency = c2.selectbox("Currency", CURRENCIES)
    if st.form_submit_button("Add account") and name.strip():
        with db() as s:
            if s.scalars(select(Account).where(Account.name == name.strip())).first():
                st.error("An account with that name already exists.")
            else:
                s.add(Account(name=name.strip(), currency=currency))
                st.rerun()

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
                with db() as s:
                    tag = s.get(Tag, tag_id)
                    if tag is not None:
                        s.delete(tag)
                st.rerun()
        with st.form(f"new_tag_{category.value}", clear_on_submit=True):
            new_tag = st.text_input("New tag", key=f"new_tag_input_{category.value}")
            if st.form_submit_button("Add") and new_tag.strip():
                with db() as s:
                    exists = s.scalars(
                        select(Tag).where(Tag.name == new_tag.strip(), Tag.category == category)
                    ).first()
                    if exists is None:
                        s.add(Tag(name=new_tag.strip(), category=category))
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
            with db() as s:
                preset = s.get(BrokerPreset, preset_id)
                if preset is not None:
                    s.delete(preset)
            st.rerun()
