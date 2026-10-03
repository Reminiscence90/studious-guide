"""Trade detail (placeholder until milestone 5)."""

import streamlit as st

st.title("Trade")
st.write(f"Trade #{st.session_state.get('selected_trade_id')}")
