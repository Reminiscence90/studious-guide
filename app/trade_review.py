"""Trade review chart for the trade detail page (TradingView Lightweight Charts™)."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.common import db, fmt_money
from app.lwc_chart import render_chart
from core.demo_market import demo_fetcher
from core.importer import load_specs
from core.instruments import default_chart_symbol, resolve
from core.market_data import (
    INTERVALS,
    MarketDataError,
    auto_interval,
    fetch_yahoo,
    get_bars,
    import_ohlc,
    local_intervals,
    review_window,
    store_bars,
    to_utc,
    utcnow,
)
from core.models import Trade
from core.review import ExecutionPoint, chart_payload, excursions, planned_rr

SOURCES = {
    "yahoo": "Yahoo Finance",
    "csv": "your uploaded candles",
    "demo": "synthetic demo candles (sample data only)",
}


def _fmt_r(value: float | None) -> str:
    return "–" if value is None else f"{value:.2f}R"


def render_trade_review(trade_id: int) -> None:
    with db() as s:
        trade = s.scalars(
            select(Trade)
            .where(Trade.id == trade_id)
            .options(selectinload(Trade.executions), selectinload(Trade.account))
        ).one()
        spec = resolve(trade.symbol, load_specs(s))
        tz = trade.account.timezone or "UTC"
        is_demo = bool(trade.account.is_demo)
        fills = [
            ExecutionPoint(e.timestamp, e.side.value, e.quantity, e.price) for e in trade.executions
        ]
        t = {
            "side": trade.side.value,
            "entry_time": trade.entry_time,
            "exit_time": trade.exit_time,
            "avg_entry": trade.avg_entry_price,
            "avg_exit": trade.avg_exit_price,
            "stop": trade.stop_loss,
            "target": trade.take_profit,
            "qty": trade.quantity,
            "pv": trade.point_value,
        }
    chart_symbol = default_chart_symbol(spec)
    upload_key = f"upload:{spec.symbol}"  # your own candles for this instrument
    now = utcnow()
    entry_utc = to_utc(t["entry_time"], tz)
    exit_utc = to_utc(t["exit_time"], tz) if t["exit_time"] else None

    st.subheader("Chart review")
    c1, c2, c3 = st.columns([2, 3, 2], vertical_alignment="bottom")
    with db() as s:
        uploaded = local_intervals(
            s, upload_key, entry_utc - timedelta(days=1), (exit_utc or now) + timedelta(days=1)
        )
    auto = auto_interval(
        entry_utc, exit_utc, now, history_limits=not is_demo, local_intervals=uploaded
    )
    choice = c1.selectbox("Interval", ["Auto", *INTERVALS], key=f"review_interval_{trade_id}")
    interval = auto if choice == "Auto" else choice
    candles_from = (
        "synthetic demo candles"
        if is_demo
        else f"your uploaded **{spec.symbol}** candles where available, otherwise "
        f"**{chart_symbol}** from Yahoo Finance"
    )
    c2.caption(
        (f"Auto picked **{interval}** · " if choice == "Auto" else "")
        + f"{candles_from} · times in **{tz}** (account time zone, set in Settings)."
    )
    refresh = c3.button("↻ Download again", key=f"review_refresh_{trade_id}", disabled=is_demo)

    start, end = review_window(entry_utc, exit_utc, interval, now)
    error = None
    with db() as s:
        bars, source = get_bars(s, upload_key, interval, start, end, fetch=None)
    if bars.empty:
        try:
            with db() as s:
                if refresh:
                    fresh = fetch_yahoo(chart_symbol, interval, start, end)
                    store_bars(s, chart_symbol, interval, fresh, "yahoo")
                fetch = demo_fetcher(s) if is_demo else fetch_yahoo
                bars, source = get_bars(s, chart_symbol, interval, start, end, fetch=fetch)
        except MarketDataError as exc:
            error = str(exc)
            with db() as s:
                bars, source = get_bars(s, chart_symbol, interval, start, end, fetch=None)

    if bars.empty:
        st.info(
            f"No {interval} candles for **{chart_symbol}** around this trade"
            + (f" — {error}" if error else "")
            + ". Yahoo Finance keeps 1-minute bars for about 30 days and 5–30 minute bars "
            "for 60 days; pick a coarser interval for older trades, or upload your own "
            "candles below."
        )
    else:
        if error:
            st.caption(f"⚠️ Showing cached candles; download failed: {error}")
        payload = chart_payload(
            bars,
            interval=interval,
            tz_name=tz,
            side=t["side"],
            entry_time=t["entry_time"],
            exit_time=t["exit_time"],
            executions=fills,
            avg_entry=t["avg_entry"],
            avg_exit=t["avg_exit"],
            stop_loss=t["stop"],
            take_profit=t["target"],
        )
        render_chart(payload)
        note = SOURCES.get(source or "", source or "")
        if source == "csv":
            note = f"your uploaded {spec.symbol} candles"
        elif source == "yahoo":
            note += f" ({chart_symbol})"
        if source == "yahoo" and spec.symbol in {"XAUUSD", "XAGUSD", "USOIL", "UKOIL"}:
            note += f" — {chart_symbol} is the futures contract and trades a little away "
            note += "from your broker's spot price; upload your broker's candles for exact levels"
        st.caption(f"Source: {note}. Markers show each fill; lines show entry, exit, stop and "
                   "target.")  # fmt: skip

        ex = excursions(
            bars,
            side=t["side"],
            entry_utc=entry_utc,
            exit_utc=exit_utc,
            interval=interval,
            avg_entry=t["avg_entry"],
            avg_exit=t["avg_exit"],
            quantity=t["qty"],
            point_value=t["pv"],
            stop_loss=t["stop"],
        )
        rr = planned_rr(t["side"], t["avg_entry"], t["stop"], t["target"])
        m1, m2, m3, m4 = st.columns(4)
        if ex is not None:
            m1.metric(
                "Max favourable (MFE)",
                fmt_money(ex.mfe_usd, signed=False)
                + (f" · {_fmt_r(ex.mfe_r)}" if ex.mfe_r is not None else ""),
                help=f"Best price {ex.best_price:g} while the trade was open "
                f"({interval} bars, so the entry and exit bars count in full).",
            )
            m2.metric(
                "Max adverse (MAE)",
                fmt_money(-ex.mae_usd)
                + (f" · {_fmt_r(-ex.mae_r)}" if ex.mae_r is not None else ""),
                help=f"Worst price {ex.worst_price:g} while the trade was open.",
            )
            m3.metric(
                "Move captured",
                "–" if ex.capture is None else f"{ex.capture:.0%}",
                help="Exit gain ÷ MFE: how much of the best move a winning exit kept. "
                "Not shown for losing trades.",
            )
        m4.metric(
            "Planned R:R",
            "–" if rr is None else f"{rr:.2f}",
            help="(target − entry) ÷ (entry − stop). Set the stop and target under Risk.",
        )

    with st.expander("Use your own candles (CSV)"):
        st.caption(
            "Upload OHLC bars for this instrument, e.g. TradingView → *Export chart data*, "
            "or your broker's MT5 export. Needs time, open, high, low and close columns; "
            "volume is optional. Unix timestamps are read as UTC, text times in the time "
            "zone below. Uploaded candles are used instead of Yahoo Finance for "
            f"**{spec.symbol}**, at every interval they can build."
        )
        upload = st.file_uploader(
            "OHLC CSV", type=["csv"], key=f"ohlc_upload_{trade_id}", label_visibility="collapsed"
        )
        csv_tz = st.text_input("Time zone of text times", value=tz, key=f"ohlc_tz_{trade_id}")
        if upload is not None and st.button("Import candles", key=f"ohlc_import_{trade_id}"):
            try:
                raw = pd.read_csv(upload)
                with db() as s:
                    got_interval, n = import_ohlc(s, upload_key, raw, csv_tz)
            except (MarketDataError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.success(f"Imported {n} {got_interval} candles for {spec.symbol}.")
                st.rerun()
