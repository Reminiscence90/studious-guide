"""Streamlit entry point: ``uv run streamlit run app/main.py``."""

import streamlit as st

from core.db import init_db

st.set_page_config(page_title="Trade Journal", page_icon="📈", layout="wide")
init_db()
st.title("Trade Journal")
st.write("Scaffold ready.")
