"""Accounts, instruments, tags and broker presets."""

from __future__ import annotations

import os
from zoneinfo import available_timezones

import pandas as pd
import streamlit as st
from sqlalchemy import func, select

from app.common import db
from core.data_sources import (
    SETTING_ENV,
    SOURCE_LABELS,
    credentials,
    set_setting,
    settings_from_db,
)
from core.importer import recalculate_trades, replace_instruments
from core.instruments import ACCOUNT_CURRENCY, AssetClass, InstrumentSpec
from core.models import Account, BrokerPreset, Instrument, Tag, TagCategory, Trade, trade_tags

st.title("Settings")


def _text(value: object) -> str:
    """Cell text from the data editor; empty cells come back as None or NaN."""
    return "" if value is None or pd.isna(value) else str(value).strip()


COMMON_ZONES = [
    "UTC",
    "Asia/Kuala_Lumpur",
    "Asia/Singapore",
    "Asia/Hong_Kong",
    "Asia/Tokyo",
    "Australia/Sydney",
    "Europe/London",
    "Europe/Athens",
    "America/New_York",
    "America/Chicago",
]
ZONES = COMMON_ZONES + sorted(z for z in available_timezones() if z not in COMMON_ZONES)

# ------------------------------------------------------------------ accounts
st.header("Accounts")
st.caption(
    f"All accounts are in {ACCOUNT_CURRENCY}. The time zone is the one your broker's "
    "timestamps are in; it lines trades up with the candles on the review chart."
)
with db() as s:
    rows = s.execute(
        select(Account, func.count(Trade.id))
        .outerjoin(Trade)
        .group_by(Account.id)
        .order_by(Account.id)
    ).all()
    account_rows = [(a.id, a.name, a.timezone or "UTC", n) for a, n in rows]

for acc_id, name, tz, n_trades in account_rows:
    with st.expander(f"{name} · {tz} · {n_trades} trades"):
        with st.form(f"acc_{acc_id}"):
            new_name = st.text_input("Name", value=name)
            new_tz = st.selectbox(
                "Time zone of the broker's timestamps",
                ZONES,
                index=ZONES.index(tz) if tz in ZONES else 0,
            )
            if st.form_submit_button("Save"):
                with db() as s:
                    acc = s.get(Account, acc_id)
                    assert acc is not None
                    acc.name = new_name.strip() or name
                    acc.timezone = new_tz
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
    name = st.text_input("Name")
    new_account_tz = st.selectbox("Time zone of the broker's timestamps", ZONES)
    if st.form_submit_button("Add account") and name.strip():
        with db() as s:
            if s.scalars(select(Account).where(Account.name == name.strip())).first():
                st.error("An account with that name already exists.")
            else:
                s.add(Account(name=name.strip(), timezone=new_account_tz))
                st.rerun()

# ------------------------------------------------------------------ market data
st.header("Market data")
st.caption(
    "Candles for the trade review chart. With nothing set up, crypto comes from Binance "
    "and everything else from Yahoo Finance (gold as the futures contract, intraday "
    "history limited to 30–60 days). Add a free OANDA token or Twelve Data key for spot "
    "forex and gold with years of 1-minute history. Keys are stored in your local "
    "database. TradingView has no data API: export candles from a TradingView chart and "
    "upload them on the trade page instead."
)
with db() as s:
    creds = credentials(s)
    stored = settings_from_db(s)
env_set = {k for k, env in SETTING_ENV.items() if os.environ.get(env)}
with st.form("market_data"):
    c1, c2 = st.columns([3, 1])
    oanda_token = c1.text_input(
        "OANDA API token",
        value=stored.get("oanda_token", ""),
        type="password",
        help="Forex, spot XAUUSD/XAGUSD and oil CFDs (bid prices). Free: open an OANDA "
        "practice (demo) account, then Manage API Access → Generate. "
        "Or set the OANDA_API_TOKEN environment variable.",
    )
    oanda_env = c2.selectbox(
        "OANDA account",
        ["practice", "live"],
        index=1 if creds["oanda_environment"] == "live" else 0,
    )
    td_key = st.text_input(
        "Twelve Data API key",
        value=stored.get("twelvedata_api_key", ""),
        type="password",
        help="Forex, spot gold, crypto and US stocks. Free key (800 requests a day) at "
        "twelvedata.com. Or set the TWELVEDATA_API_KEY environment variable.",
    )
    if env_set:
        names = ", ".join(SETTING_ENV[k] for k in sorted(env_set))
        st.caption(f"Set by environment variable (overrides the fields): {names}")
    if st.form_submit_button("Save market data settings"):
        with db() as s:
            set_setting(s, "oanda_token", oanda_token)
            set_setting(s, "oanda_environment", oanda_env)
            set_setting(s, "twelvedata_api_key", td_key)
        st.success("Saved.")
        st.rerun()
active = []
if creds["oanda_token"]:
    active.append("OANDA for forex and spot metals")
elif creds["twelvedata_api_key"]:
    active.append("Twelve Data for forex and spot metals")
active += ["Binance for crypto", "Yahoo Finance for stocks and futures"]
st.caption("Auto uses: " + "; ".join(active) + ".")

# ------------------------------------------------------------------ instruments
st.header("Instruments")
st.caption(
    "P&L = price move × quantity × multiplier. Futures: quantity in contracts, multiplier = "
    "$ per point (ES 50, NQ 20, CL 1,000). Forex: quantity in lots, multiplier 100,000 "
    "(set 1 if your broker exports units); USD-base pairs such as USDJPY are converted to "
    "USD at the exit price. Futures contract codes (ESZ6, MNQH27) match their root. "
    "Broker-suffixed symbols are treated as their base (XAUUSD.R → XAUUSD, a commodity; "
    "EURUSD.R → EURUSD, forex). Symbols "
    "not listed are treated as US stocks with multiplier 1."
)
with db() as s:
    instruments = pd.DataFrame(
        [
            {
                "symbol": i.symbol,
                "name": i.name,
                "asset_class": i.asset_class.value,
                "multiplier": i.multiplier,
                "quote_currency": i.quote_currency,
                "data_source": i.data_source or "auto",
                "chart_symbol": i.chart_symbol or "",
            }
            for i in s.scalars(
                select(Instrument).order_by(Instrument.asset_class, Instrument.symbol)
            )
        ],
        columns=[
            "symbol",
            "name",
            "asset_class",
            "multiplier",
            "quote_currency",
            "data_source",
            "chart_symbol",
        ],
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
        "data_source": st.column_config.SelectboxColumn(
            "Chart source",
            options=list(SOURCE_LABELS),
            help="Where review-chart candles come from. Auto: crypto → Binance; forex and "
            "spot metals → OANDA or Twelve Data when set up; the rest → Yahoo Finance.",
        ),
        "chart_symbol": st.column_config.TextColumn(
            "Chart ticker",
            help="Ticker in the source's format; leave empty for the default. Examples: "
            "OANDA XAU_USD, EUR_USD · Twelve Data XAU/USD, EUR/USD · Binance BTCUSDT · "
            "Yahoo ES=F, EURUSD=X, TSLA.",
        ),
    },
    key="instrument_editor",
)
if st.button("💾 Save instruments and recalculate P&L"):
    specs = [
        InstrumentSpec(
            symbol=str(r.symbol),
            asset_class=AssetClass(r.asset_class),
            multiplier=float(r.multiplier),
            quote_currency=_text(r.quote_currency) or "USD",
            chart_symbol=_text(r.chart_symbol),
            data_source=_text(r.data_source),
            name=_text(r.name),
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
