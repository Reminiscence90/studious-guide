"""Plotly figure builders. Each takes metric outputs (DataFrames) and returns a figure."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from core.metrics import JournalScore

BLUE = "#2a78d6"
POSITIVE = "#1baf7a"
NEGATIVE = "#e34948"
GRID = "rgba(128,128,128,0.15)"


def _layout(fig: go.Figure, height: int = 340, **kwargs: object) -> go.Figure:
    fig.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        hovermode="x unified",
        showlegend=False,
        **kwargs,
    )
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor=GRID, zerolinecolor="rgba(128,128,128,0.4)")
    return fig


def empty_figure(message: str = "No closed trades in this range", height: int = 300) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font={"size": 14})
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return _layout(fig, height)


def equity_curve_figure(curve: pd.DataFrame, currency: str) -> go.Figure:
    """Cumulative P&L line with the drawdown shaded beneath the running peak."""
    if curve.empty:
        return empty_figure()
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=curve["exit_time"],
            y=curve["peak"],
            mode="lines",
            line={"width": 0},
            hoverinfo="skip",
            name="Peak",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=curve["exit_time"],
            y=curve["cumulative_pnl"],
            mode="lines",
            line={"color": BLUE, "width": 2},
            fill="tonexty",
            fillcolor="rgba(227,73,72,0.12)",
            name="Cumulative P&L",
            hovertemplate=f"%{{y:,.2f}} {currency}<extra>Cumulative P&L</extra>",
        )
    )
    return _layout(fig, yaxis_title=f"Cumulative P&L ({currency})")


def daily_pnl_figure(daily: pd.DataFrame, currency: str) -> go.Figure:
    if daily.empty:
        return empty_figure()
    colors = [POSITIVE if v >= 0 else NEGATIVE for v in daily["net_pnl"]]
    fig = go.Figure(
        go.Bar(
            x=pd.to_datetime(daily["date"]),
            y=daily["net_pnl"],
            marker={"color": colors, "cornerradius": 4},
            customdata=daily[["trades", "wins"]],
            hovertemplate=(
                f"%{{x|%a %d %b %Y}}<br>%{{y:+,.2f}} {currency}"
                "<br>%{customdata[0]} trades, %{customdata[1]} winners<extra></extra>"
            ),
        )
    )
    return _layout(fig, yaxis_title=f"Net P&L ({currency})", bargap=0.25)


def journal_score_radar(score: JournalScore) -> go.Figure:
    labels = list(score.components)
    values = list(score.components.values())
    fig = go.Figure(
        go.Scatterpolar(
            r=[*values, values[0]],
            theta=[*labels, labels[0]],
            fill="toself",
            fillcolor="rgba(42,120,214,0.2)",
            line={"color": BLUE, "width": 2},
            marker={"size": 8},
            hovertemplate="%{theta}: %{r:.0f}/100<extra></extra>",
        )
    )
    fig.update_layout(
        polar={
            "radialaxis": {"range": [0, 100], "tickvals": [25, 50, 75, 100], "gridcolor": GRID},
            "angularaxis": {"gridcolor": GRID},
        },
        height=340,
        margin={"l": 50, "r": 50, "t": 30, "b": 30},
        showlegend=False,
    )
    return fig


def breakdown_bar_figure(
    table: pd.DataFrame,
    currency: str,
    *,
    horizontal: bool = False,
    height: int = 320,
) -> go.Figure:
    """Net P&L per group, colored by sign. ``table`` is a ``metrics.breakdown`` result."""
    if table.empty:
        return empty_figure("No data")
    groups = table["group"].astype(str)
    values = table["net_pnl"].astype(float)
    colors = [POSITIVE if v >= 0 else NEGATIVE for v in values]
    hover = (
        "%{customdata[0]}<br>%{customdata[1]:+,.2f} " + currency + "<br>%{customdata[2]} trades, "
        "%{customdata[3]:.0%} win rate<extra></extra>"
    )
    custom = pd.DataFrame(
        {
            "g": groups,
            "pnl": values,
            "n": table["trades"],
            "wr": table["win_rate"].astype(float).fillna(0),
        }
    )
    bar = go.Bar(
        x=values if horizontal else groups,
        y=groups if horizontal else values,
        orientation="h" if horizontal else "v",
        marker={"color": colors, "cornerradius": 4},
        customdata=custom,
        hovertemplate=hover,
    )
    fig = go.Figure(bar)
    _layout(fig, height, hovermode="closest", bargap=0.3)
    if horizontal:
        fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
        fig.update_xaxes(showgrid=True, gridcolor=GRID, title=f"Net P&L ({currency})")
    else:
        fig.update_yaxes(title=f"Net P&L ({currency})")
        fig.update_xaxes(type="category")
    return fig
