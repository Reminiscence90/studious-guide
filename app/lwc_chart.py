"""Render a TradingView Lightweight Charts™ candlestick chart inside Streamlit.

The library is vendored in ``app/static`` and inlined into a sandboxed component, so the
chart needs no CDN. The payload comes from ``core.review.chart_payload``.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

LIB = Path(__file__).parent / "static" / "lightweight-charts.standalone.production.js"

THEMES = {
    "light": {"bg": "#ffffff", "text": "#3d3d3a", "grid": "rgba(128,128,128,0.12)"},
    "dark": {"bg": "#0e1117", "text": "#d6d6d0", "grid": "rgba(128,128,128,0.18)"},
}

TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html, body { margin: 0; padding: 0; background: __BG__; }
  #chart { position: relative; width: 100%; height: __HEIGHT__px; }
  #legend { position: absolute; left: 10px; top: 6px; z-index: 3; font: 12px/1.4
    "Source Sans Pro", system-ui, sans-serif; color: __TEXT__; pointer-events: none; }
</style>
<script>__LIB__</script>
</head><body>
<div id="chart"><div id="legend"></div></div>
<script>
const data = __DATA__;
const theme = __THEME__;
const LC = LightweightCharts;
const el = document.getElementById('chart');
const chart = LC.createChart(el, {
  autoSize: true,
  layout: { background: { color: theme.bg }, textColor: theme.text, attributionLogo: true },
  grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
  rightPriceScale: { borderVisible: false },
  timeScale: { borderVisible: false, timeVisible: data.intraday, secondsVisible: false },
  crosshair: { mode: LC.CrosshairMode.Normal },
});
const candles = chart.addSeries(LC.CandlestickSeries, {
  upColor: '#1baf7a', downColor: '#e34948', borderVisible: false,
  wickUpColor: '#1baf7a', wickDownColor: '#e34948',
  priceFormat: { type: 'price', precision: data.precision, minMove: data.minMove },
  // Keep entry, exit, stop and target on screen even when price never reached them.
  autoscaleInfoProvider: (original) => {
    const res = original();
    if (!res || !data.lines.length) return res;
    const levels = data.lines.map((l) => l.price);
    res.priceRange.minValue = Math.min(res.priceRange.minValue, ...levels);
    res.priceRange.maxValue = Math.max(res.priceRange.maxValue, ...levels);
    return res;
  },
});
candles.setData(data.candles);
if (data.volume.length) {
  const vol = chart.addSeries(LC.HistogramSeries, {
    priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false,
    priceLineVisible: false,
  });
  vol.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  vol.setData(data.volume);
}
for (const l of data.lines) {
  candles.createPriceLine({ price: l.price, color: l.color, lineWidth: 2, lineStyle: l.style,
                            axisLabelVisible: true, title: l.title });
}
LC.createSeriesMarkers(candles, data.markers);
if (data.visible) {
  try { chart.timeScale().setVisibleRange(data.visible); }
  catch (e) { chart.timeScale().fitContent(); }
} else { chart.timeScale().fitContent(); }

const legend = document.getElementById('legend');
const fmt = (v) => v.toFixed(data.precision);
chart.subscribeCrosshairMove((p) => {
  const c = p && p.seriesData ? p.seriesData.get(candles) : null;
  legend.textContent = c
    ? `O ${fmt(c.open)}  H ${fmt(c.high)}  L ${fmt(c.low)}  C ${fmt(c.close)}` : '';
});
</script></body></html>"""


@cache
def _library() -> str:
    return LIB.read_text(encoding="utf-8")


def theme_name() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def build_html(payload: dict[str, Any], height: int = 460, theme: str = "light") -> str:
    colors = THEMES.get(theme, THEMES["light"])
    return (
        TEMPLATE.replace("__LIB__", _library())
        .replace("__DATA__", json.dumps(payload))
        .replace("__THEME__", json.dumps(colors))
        .replace("__BG__", colors["bg"])
        .replace("__TEXT__", colors["text"])
        .replace("__HEIGHT__", str(height))
    )


def render_chart(payload: dict[str, Any], height: int = 460) -> None:
    components.html(build_html(payload, height, theme_name()), height=height + 4)
