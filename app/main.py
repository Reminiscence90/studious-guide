"""Streamlit entry point: ``uv run streamlit run app/main.py``."""

from __future__ import annotations

import streamlit as st

from app.common import render_sidebar_filters

st.set_page_config(page_title="Trade Journal", page_icon="📈", layout="wide")

PAGES = {
    "Analytics": [
        st.Page("dashboard.py", title="Dashboard", icon="📊", url_path="dashboard", default=True),
        st.Page("calendar_view.py", title="Calendar", icon="📅", url_path="calendar"),
    ],
    "Journal": [
        st.Page("trade_log.py", title="Trade log", icon="📋", url_path="trades"),
        st.Page("trade_detail.py", title="Trade detail", icon="🔎", url_path="trade"),
        st.Page("journal.py", title="Daily journal", icon="📓", url_path="journal"),
        st.Page("playbooks.py", title="Playbooks", icon="📘", url_path="playbooks"),
    ],
    "Data": [
        st.Page("import_trades.py", title="Import", icon="⬆️", url_path="import"),
        st.Page("settings.py", title="Settings", icon="⚙️", url_path="settings"),
    ],
}

navigation = st.navigation(PAGES)
st.session_state["filters"] = render_sidebar_filters()
navigation.run()
