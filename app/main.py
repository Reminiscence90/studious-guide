"""Streamlit entry point: ``uv run streamlit run app/main.py``."""

from __future__ import annotations

import streamlit as st

from app.common import render_sidebar_filters

st.set_page_config(page_title="Trade Journal", page_icon="📈", layout="wide")

PAGES = {
    "Data": [
        st.Page("import_trades.py", title="Import", icon="⬆️", default=True),
        st.Page("settings.py", title="Settings", icon="⚙️"),
    ],
}

navigation = st.navigation(PAGES)
st.session_state["filters"] = render_sidebar_filters()
navigation.run()
