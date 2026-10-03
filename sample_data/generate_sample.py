"""Generate ``sample_trades.csv``: ~200 realistic USD trades across US stocks, futures,
forex, commodities and crypto, with TSLA as the most-traded symbol.

Run with ``uv run python sample_data/generate_sample.py``. Output is deterministic.

The CSV uses broker-style headers (Ticker, Direction, Open Time, ...) so the
column-mapping screen has something to do. Futures use contract codes (``ESU6``),
which the importer resolves to their root (``ES``, $50/point). Roughly one in five
trades is split across several rows (scaling out of a position), and the importer
groups those back into a single trade. Times are US Eastern.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

OUTPUT = Path(__file__).with_name("sample_trades.csv")
HEADERS = [
    "Ticker",
    "Direction",
    "Qty",
    "Entry Price",
    "Open Time",
    "Exit Price",
    "Close Time",
    "Commission",
]


@dataclass(frozen=True)
class Market:
    symbol: str  # futures: root, the contract code is added per date
    asset: str  # stock | future | forex | commodity | crypto
    price: float  # approximate price
    point_value: float  # USD per 1.0 move per unit (must match core.instruments)
    vol: float  # typical intraday move as a fraction of price
    tick: float
    qty_step: float
    weight: float  # how often it is traded
    fee: float  # per unit per side (stocks: per share; crypto: fraction of notional)


MARKETS = [
    Market("TSLA", "stock", 430.0, 1, 0.022, 0.01, 1, 9.0, 0.005),
    Market("NVDA", "stock", 180.0, 1, 0.016, 0.01, 1, 2.0, 0.005),
    Market("AAPL", "stock", 235.0, 1, 0.010, 0.01, 1, 1.5, 0.005),
    Market("ES", "future", 6500.0, 50, 0.004, 0.25, 1, 1.5, 2.25),
    Market("MES", "future", 6500.0, 5, 0.004, 0.25, 1, 1.5, 0.62),
    Market("MNQ", "future", 23500.0, 2, 0.006, 0.25, 1, 2.5, 0.62),
    Market("CL", "commodity", 66.0, 1000, 0.012, 0.01, 1, 1.5, 2.25),
    Market("GC", "commodity", 3450.0, 100, 0.004, 0.1, 1, 1.0, 2.25),
    Market("XAUUSD", "commodity", 3440.0, 100, 0.006, 0.01, 0.01, 2.0, 3.5),
    Market("EURUSD", "forex", 1.165, 100_000, 0.0035, 0.00001, 0.01, 2.5, 3.5),
    Market("GBPUSD", "forex", 1.345, 100_000, 0.004, 0.00001, 0.01, 1.5, 3.5),
    Market("USDJPY", "forex", 147.5, 100_000, 0.004, 0.001, 0.01, 1.5, 3.5),
    Market("BTCUSD", "crypto", 112_000.0, 1, 0.015, 0.01, 0.001, 2.0, 0.0006),
    Market("ETHUSD", "crypto", 4300.0, 1, 0.02, 0.01, 0.01, 1.5, 0.0006),
]

QUARTER_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}
MONTH_CODES = "FGHJKMNQUVXZ"


def contract_code(m: Market, day: date) -> str:
    """Front-month futures contract, e.g. ES on 2026-07-01 → ``ESU6``."""
    if m.symbol in ("ES", "MES", "MNQ"):
        month = next(q for q in (3, 6, 9, 12) if (q, 12) > (day.month, day.day))
        return f"{m.symbol}{QUARTER_CODES[month]}{day.year % 10}"
    if m.symbol == "CL":  # trades the next calendar month
        nxt = day.month % 12 + 1
        return f"CL{MONTH_CODES[nxt - 1]}{(day.year + (day.month == 12)) % 10}"
    if m.symbol == "GC":  # even months
        month = next(q for q in (2, 4, 6, 8, 10, 12) if q > day.month) if day.month < 12 else 2
        return f"GC{MONTH_CODES[month - 1]}{(day.year + (day.month == 12)) % 10}"
    return m.symbol


def round_to(value: float, step: float) -> float:
    decimals = max(0, len(f"{step:.10f}".rstrip("0").split(".")[1]))
    return round(round(value / step) * step, decimals)


def fees_for(m: Market, qty: float, price: float) -> float:
    """Commission for one side."""
    match m.asset:
        case "stock":
            return max(1.0, qty * m.fee)
        case "crypto":
            return qty * price * m.fee
        case _:
            return qty * m.fee  # per contract / per lot


def session_time(rng: random.Random, m: Market, day: date) -> datetime:
    """Entry time in US Eastern time for the market's typical session."""
    if m.asset == "stock":
        start, end = time(9, 30), time(15, 45)
        skew = 1.8  # most activity near the open
    elif m.asset == "crypto":
        start, end, skew = time(0, 0), time(23, 30), 1.0
    else:  # futures, forex, commodities: London + New York
        start, end, skew = time(3, 0), time(15, 30), 1.2
    start_dt = datetime.combine(day, start)
    span = (datetime.combine(day, end) - start_dt).total_seconds()
    return start_dt + timedelta(seconds=int(span * rng.random() ** skew))


def trading_days(start: date, end: date, weekends: bool) -> list[date]:
    days, d = [], start
    while d <= end:
        if weekends or d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def fmt_price(m: Market, price: float) -> str:
    decimals = len(f"{m.tick:.10f}".rstrip("0").split(".")[1])
    return f"{price:.{decimals}f}"


def fmt_qty(m: Market, qty: float) -> str:
    return f"{qty:g}"


def generate(seed: int = 42, n_trades: int = 245) -> list[dict[str, str]]:
    rng = random.Random(seed)
    weekdays = trading_days(date(2026, 5, 4), date(2026, 9, 30), weekends=False)
    all_days = trading_days(date(2026, 5, 4), date(2026, 9, 30), weekends=True)
    rows: list[dict[str, str]] = []
    # A symbol is busy until its open position is closed, so trades never overlap
    # on the same symbol (overlapping fills would correctly be merged into one trade).
    busy_until: dict[str, datetime] = {}
    drift = {m.symbol: 1.0 for m in MARKETS}

    plans = []
    for _ in range(n_trades):
        m = rng.choices(MARKETS, weights=[mk.weight for mk in MARKETS])[0]
        day = rng.choice(all_days if m.asset == "crypto" else weekdays)
        plans.append((day, m))
    plans.sort(key=lambda p: p[0])

    for day, m in plans:
        entry_time = session_time(rng, m, day)
        symbol = contract_code(m, day)
        if busy_until.get(m.symbol, datetime.min) >= entry_time:
            continue
        drift[m.symbol] *= 1 + rng.gauss(0, m.vol * 0.6)
        entry_price = round_to(m.price * drift[m.symbol], m.tick)
        is_long = rng.random() < 0.62

        swing = rng.random() < 0.18
        if swing:
            exit_day = day + timedelta(days=rng.randint(1, 6))
            while m.asset != "crypto" and exit_day.weekday() >= 5:
                exit_day += timedelta(days=1)
            exit_time = session_time(rng, m, exit_day)
        else:
            exit_time = entry_time + timedelta(minutes=int(rng.lognormvariate(3.2, 0.9)) + 1)
            if m.asset == "stock":
                exit_time = min(exit_time, datetime.combine(day, time(15, 59)))
        busy_until[m.symbol] = exit_time

        # Size each trade to risk roughly $150–$400 at the stop.
        risk_pct = m.vol * (0.45 if not swing else 1.2)
        risk_usd = rng.uniform(150, 400)
        qty = round_to(risk_usd / (entry_price * risk_pct * m.point_value
                                   / (entry_price if m.symbol.startswith("USD") else 1)),
                       m.qty_step)  # fmt: skip
        qty = max(qty, m.qty_step)

        # A modest edge: ~46% winners, winners ~1.6x the size of losers.
        if rng.random() < 0.46:
            move = risk_pct * rng.uniform(0.5, 2.6)
        else:
            move = -risk_pct * rng.uniform(0.3, 1.2)
        direction = 1 if is_long else -1
        exit_price = round_to(entry_price * (1 + direction * move), m.tick)

        # Scale out across 2-3 rows for about one trade in five.
        units = round(qty / m.qty_step)
        parts = rng.choice([2, 3]) if rng.random() < 0.2 and units >= 3 else 1
        split_units = [units // parts] * parts
        split_units[-1] = units - sum(split_units[:-1])
        for i, part_units in enumerate(split_units):
            part_qty = round_to(part_units * m.qty_step, m.qty_step)
            if i < parts - 1:
                part_exit_price = round_to(
                    exit_price * (1 - direction * rng.uniform(0, m.vol * 0.2)), m.tick
                )
                part_exit_time = entry_time + (exit_time - entry_time) * (i + 1) / parts
            else:
                part_exit_price, part_exit_time = exit_price, exit_time
            fees = fees_for(m, part_qty, entry_price) + fees_for(m, part_qty, part_exit_price)
            rows.append(
                {
                    "Ticker": symbol,
                    "Direction": "Long" if is_long else "Short",
                    "Qty": fmt_qty(m, part_qty),
                    "Entry Price": fmt_price(m, entry_price),
                    "Open Time": entry_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "Exit Price": fmt_price(m, part_exit_price),
                    "Close Time": part_exit_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "Commission": f"{fees:.2f}",
                }
            )

    # Two positions still open at the end of the sample period. Open positions are
    # stored but excluded from all statistics. The symbols are not traded again
    # afterwards, so they stay open after grouping.
    by_symbol = {m.symbol: m for m in MARKETS}
    for sym, side, qty, price, stamp in [
        ("TSLA", "Long", 100, 452.18, "2026-10-01 10:12:44"),
        ("ETHUSD", "Short", 2, 4385.50, "2026-10-01 15:03:10"),
    ]:
        m = by_symbol[sym]
        rows.append(
            {
                "Ticker": sym,
                "Direction": side,
                "Qty": fmt_qty(m, qty),
                "Entry Price": fmt_price(m, price),
                "Open Time": stamp,
                "Exit Price": "",
                "Close Time": "",
                "Commission": f"{fees_for(m, qty, price):.2f}",
            }
        )
    return rows


def main() -> None:
    rows = generate()
    with OUTPUT.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {OUTPUT}")


if __name__ == "__main__":
    main()
