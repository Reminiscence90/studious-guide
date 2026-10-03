"""Generate ``sample_trades.csv``: ~200 realistic Bursa Malaysia trades for the demo.

Run with ``uv run python sample_data/generate_sample.py``. Output is deterministic.

The CSV deliberately uses broker-style headers (Ticker, Direction, Open Time, ...)
so the column-mapping screen has something to do, and roughly one in five trades
is split across several rows (scaling out of a position), which the importer
groups back into a single trade.
"""

from __future__ import annotations

import csv
import random
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

# Symbol -> approximate price in MYR
SYMBOLS: dict[str, float] = {
    "MAYBANK": 10.20,
    "PBBANK": 4.45,
    "CIMB": 7.90,
    "TENAGA": 13.80,
    "GAMUDA": 5.10,
    "YTLPOWR": 4.05,
    "INARI": 2.95,
    "TOPGLOV": 0.98,
    "GENTING": 4.20,
    "SUNWAY": 4.60,
    "PCHEM": 5.85,
    "IHH": 7.10,
}

# Bursa trading sessions (local time)
SESSIONS = [(time(9, 0), time(12, 30)), (time(14, 30), time(16, 45))]


def tick(price: float) -> float:
    """Round to a Bursa tick size."""
    step = 0.005 if price < 1 else 0.01 if price < 10 else 0.02
    return round(round(price / step) * step, 3)


def fees_for(value: float) -> float:
    """Approximate brokerage + clearing + stamp duty for one leg."""
    brokerage = max(8.0, value * 0.0008)
    clearing = value * 0.0003
    stamp = min(1000.0, -(-value // 1000))  # RM1 per RM1,000 or part thereof
    return round(brokerage + clearing + stamp, 2)


def random_intraday_time(rng: random.Random, day: date) -> datetime:
    start, end = rng.choices(SESSIONS, weights=[0.7, 0.3])[0]
    start_dt = datetime.combine(day, start)
    span = (datetime.combine(day, end) - start_dt).total_seconds()
    # Skew entries toward the open, where most day-trading happens.
    offset = span * rng.random() ** 1.8
    return start_dt + timedelta(seconds=int(offset))


def trading_days(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def generate(seed: int = 42, n_trades: int = 205) -> list[dict[str, str]]:
    rng = random.Random(seed)
    days = trading_days(date(2026, 5, 4), date(2026, 9, 30))
    trade_days = sorted(rng.choices(days, k=n_trades))
    rows: list[dict[str, str]] = []
    # A symbol is busy until its open position is closed, so trades never overlap
    # on the same symbol (overlapping fills would correctly be merged into one trade).
    busy_until: dict[str, datetime] = {}

    for day in trade_days:
        entry_time = random_intraday_time(rng, day)
        free = [s for s in SYMBOLS if busy_until.get(s, datetime.min) < entry_time]
        if not free:
            continue
        symbol = rng.choice(free)
        base = SYMBOLS[symbol] * (1 + rng.gauss(0, 0.04))
        is_long = rng.random() < 0.72
        entry_price = tick(base)
        lots = rng.choice([5, 10, 10, 20, 20, 30, 50])
        if entry_price < 2:
            lots *= 4
        qty = lots * 100

        swing = rng.random() < 0.22
        if swing:
            exit_day = day + timedelta(days=rng.randint(1, 9))
            while exit_day.weekday() >= 5:
                exit_day += timedelta(days=1)
            exit_time = random_intraday_time(rng, exit_day)
        else:
            exit_time = entry_time + timedelta(minutes=int(rng.lognormvariate(3.0, 1.0)) + 1)
            close = datetime.combine(day, time(16, 50))
            exit_time = min(exit_time, close)

        busy_until[symbol] = exit_time

        # A modest edge: ~52% winners, winners ~1.5x the size of losers.
        risk_pct = 0.012 if not swing else 0.03
        if rng.random() < 0.52:
            move = risk_pct * rng.uniform(0.6, 3.0)
        else:
            move = -risk_pct * rng.uniform(0.3, 1.25)
        direction = 1 if is_long else -1
        exit_price = tick(entry_price * (1 + direction * move))

        # Scale out across 2-3 rows for about one trade in five.
        parts = 1
        if rng.random() < 0.2 and lots >= 10:
            parts = rng.choice([2, 3])
        split_qty = [qty // parts // 100 * 100] * parts
        split_qty[-1] = qty - sum(split_qty[:-1])
        part_exit_time = exit_time
        for i, part_qty in enumerate(split_qty):
            part_exit_price = exit_price
            if i < parts - 1:
                # Earlier partial exits happen before the final exit at a slightly
                # different price.
                part_exit_price = tick(exit_price * (1 - direction * rng.uniform(0, 0.004)))
                part_exit_time = entry_time + (exit_time - entry_time) * (i + 1) / parts
            else:
                part_exit_time = exit_time
            value = part_qty * (entry_price + part_exit_price)
            rows.append(
                {
                    "Ticker": symbol,
                    "Direction": "Long" if is_long else "Short",
                    "Qty": str(part_qty),
                    "Entry Price": f"{entry_price:.3f}",
                    "Open Time": entry_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "Exit Price": f"{part_exit_price:.3f}",
                    "Close Time": part_exit_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "Commission": f"{fees_for(value / 2) * 2:.2f}",
                }
            )

    # Two positions that are still open at the end of the sample period. Open
    # positions are stored but excluded from all statistics. The symbols are not
    # traded again afterwards, so they stay open after grouping.
    for symbol, side, qty, price, stamp in [
        ("GENTING", "Long", 2000, 4.31, "2026-09-30 10:12:44"),
        ("GAMUDA", "Short", 2000, 5.12, "2026-09-30 15:03:10"),
    ]:
        rows.append(
            {
                "Ticker": symbol,
                "Direction": side,
                "Qty": str(qty),
                "Entry Price": f"{price:.3f}",
                "Open Time": stamp,
                "Exit Price": "",
                "Close Time": "",
                "Commission": f"{fees_for(qty * price):.2f}",
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
