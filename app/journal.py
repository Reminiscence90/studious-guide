"""Daily journal (placeholder until milestone 6)."""

import streamlit as st

st.title("Journal")
st.write(st.session_state.get("journal_day"))
