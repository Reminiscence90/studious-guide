"""Strategy playbooks: rules, checklist and per-playbook performance."""

from __future__ import annotations

import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.charts import breakdown_bar_figure, equity_curve_figure
from app.common import (
    currency_warning,
    db,
    fmt_money,
    fmt_pct,
    fmt_ratio,
    get_filters,
    load_filtered_trades,
)
from core import metrics as m
from core.journal import save_playbook
from core.models import Playbook

filters = get_filters()
cur = filters.currency
df = load_filtered_trades(filters)

st.title("Playbooks")
currency_warning(filters)

with db() as s:
    playbooks = [
        (p.id, p.name, p.description, p.entry_rules, p.exit_rules,
         [i.text for i in p.checklist_items])
        for p in s.scalars(
            select(Playbook).options(selectinload(Playbook.checklist_items)).order_by(Playbook.name)
        )
    ]  # fmt: skip

# ------------------------------------------------------------------ overview
stats = m.playbook_stats(df)
if not stats.empty:
    st.subheader("Performance by playbook")
    table = stats.rename(columns={"group": "Playbook"})
    st.dataframe(
        table[["Playbook", "trades", "win_rate", "net_pnl", "avg_pnl", "profit_factor"]],
        hide_index=True,
        width="stretch",
        column_config={
            "trades": "Trades taken",
            "win_rate": st.column_config.NumberColumn("Win rate", format="percent"),
            "net_pnl": st.column_config.NumberColumn(f"Net P&L ({cur})", format="%+,.2f"),
            "avg_pnl": st.column_config.NumberColumn("Expectancy / trade", format="%+,.2f"),
            "profit_factor": st.column_config.NumberColumn("Profit factor", format="%.2f"),
        },
    )
    untracked = m.closed_trades(df)
    untracked = untracked[untracked["playbook"].isna()] if not untracked.empty else untracked
    if len(untracked):
        st.caption(
            f"{len(untracked)} trades are not linked to a playbook "
            f"(net {fmt_money(m.net_pnl(untracked), cur)})."
        )


def playbook_form(key: str, values: tuple | None = None) -> None:
    pid, name, desc, entry, exit_, checklist = values or (None, "", "", "", "", [])
    with st.form(key):
        new_name = st.text_input("Name", value=name)
        new_desc = st.text_area("Description", value=desc, height=80)
        c1, c2 = st.columns(2)
        new_entry = c1.text_area("Entry rules (markdown)", value=entry, height=140)
        new_exit = c2.text_area("Exit rules (markdown)", value=exit_, height=140)
        new_checklist = st.text_area(
            "Checklist — one criterion per line", value="\n".join(checklist), height=120
        )
        if st.form_submit_button("Save playbook", type="primary"):
            try:
                with db() as s:
                    save_playbook(
                        s,
                        playbook_id=pid,
                        name=new_name,
                        description=new_desc,
                        entry_rules=new_entry,
                        exit_rules=new_exit,
                        checklist=new_checklist.splitlines(),
                    )
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.toast("Playbook saved")
                st.rerun()


# ------------------------------------------------------------------ each playbook
names = [p[1] for p in playbooks]
tabs = st.tabs([*names, "➕ New playbook"])
for tab, values in zip(tabs, playbooks, strict=False):
    pid, name, desc, entry, exit_, checklist = values
    with tab:
        pb_trades = df[df["playbook_id"] == pid] if not df.empty else df
        s_ = m.summary_stats(pb_trades)
        if desc:
            st.write(desc)
        c = st.columns(5)
        c[0].metric("Trades taken", s_.total_trades, border=True)
        c[1].metric("Win rate", fmt_pct(s_.win_rate), border=True)
        c[2].metric("Net P&L", fmt_money(s_.net_pnl), border=True)
        c[3].metric("Expectancy", fmt_money(s_.expectancy), border=True)
        c[4].metric("Profit factor", fmt_ratio(s_.profit_factor), border=True)

        left, right = st.columns(2, gap="large")
        with left:
            st.markdown("**Entry rules**")
            st.markdown(entry or "_none_")
            st.markdown("**Exit rules**")
            st.markdown(exit_ or "_none_")
            st.markdown("**Checklist**")
            st.markdown("\n".join(f"- [ ] {item}" for item in checklist) or "_none_")
        with right:
            st.markdown("**Equity curve**")
            st.plotly_chart(
                equity_curve_figure(m.equity_curve(pb_trades), cur),
                width="stretch",
                key=f"pb_curve_{pid}",
            )
            adherence = m.breakdown(pb_trades, m.rule_adherence)
            if not adherence.empty:
                st.markdown("**Followed the checklist?**")
                st.plotly_chart(
                    breakdown_bar_figure(adherence, cur, horizontal=True, height=180),
                    width="stretch",
                    key=f"pb_rules_{pid}",
                )
                st.caption(
                    " · ".join(
                        f"{r.group}: {r.trades} trades, {fmt_pct(r.win_rate)} win rate"
                        for r in adherence.itertuples()
                    )
                )
        with st.expander("Edit playbook"):
            playbook_form(f"edit_pb_{pid}", values)
            if st.button("Delete playbook", key=f"del_pb_{pid}"):
                with db() as s:
                    obj = s.get(Playbook, pid)
                    if obj is not None:
                        s.delete(obj)
                st.rerun()

with tabs[-1]:
    st.caption("Link trades to a playbook from the trade detail page.")
    playbook_form("new_playbook")
