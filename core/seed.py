"""Load the sample data so the app is usable immediately.

Usage::

    uv run trade-journal-seed            # seed an empty database
    uv run trade-journal-seed --reset    # wipe the database first

Besides importing ``sample_data/sample_trades.csv`` this adds demo stop losses,
tags, playbooks (with checklist results) and daily journal entries, all generated
deterministically.
"""

from __future__ import annotations

import argparse
import random
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from core.db import PROJECT_ROOT, get_engine, init_db
from core.importer import guess_mapping, import_dataframe, save_preset
from core.instruments import AssetClass
from core.models import (
    Account,
    Base,
    JournalEntry,
    Playbook,
    PlaybookChecklistItem,
    Tag,
    TagCategory,
    Trade,
    TradeChecklistResult,
    TradeSide,
    TradeStatus,
)

SAMPLE_CSV = PROJECT_ROOT / "sample_data" / "sample_trades.csv"

PLAYBOOKS = [
    {
        "name": "Golden Ratio - 0.618 retracement",
        "description": "Buy (sell) the pullback to the 61.8% Fibonacci retracement of a clean "
        "impulse leg, in the direction of the higher-timeframe trend.",
        "entry_rules": "- Higher-timeframe (4H/1H) trend is clear\n"
        "- Identify a clean impulse leg with displacement on the 15m/1H\n"
        "- Draw the Fibonacci from swing low to swing high (longs) or high to low (shorts)\n"
        "- Wait for price to reach the 0.618 level (golden pocket 0.618–0.65)\n"
        "- Enter on a rejection candle (pin bar / engulfing) at the level",
        "exit_rules": "- Stop beyond the 0.786 retracement (or the swing origin)\n"
        "- TP1 at the prior swing high/low (0.0 level), move stop to breakeven\n"
        "- TP2 at the −0.272 extension",
        "checklist": [
            "Higher-timeframe trend aligned",
            "Clean impulse leg with displacement",
            "Price reached the 0.618 / golden pocket",
            "Rejection candle confirmed at the level",
            "Stop beyond 0.786",
            "Reward-to-risk at least 2:1",
        ],
    },
    {
        "name": "Supply/Demand zone",
        "description": "Trade the first return to a fresh supply or demand zone created by a "
        "strong departure (rally-base-drop / drop-base-rally).",
        "entry_rules": "- Mark the base candles before a strong, imbalanced departure\n"
        "- Zone must be fresh (not yet retested)\n"
        "- Prefer zones aligned with the higher-timeframe trend\n"
        "- Enter at the proximal line, or on a lower-timeframe confirmation inside the zone",
        "exit_rules": "- Stop beyond the distal line plus a small buffer\n"
        "- Target the opposing supply/demand zone\n"
        "- Take partials at 2R",
        "checklist": [
            "Fresh, untested zone",
            "Strong departure from the base",
            "Zone aligned with higher-timeframe trend",
            "Room to the opposing zone of 2R or more",
            "Stop beyond the distal line",
        ],
    },
    {
        "name": "ICT - Liquidity Sweep + BOS + Imbalance",
        "description": "Wait for a sweep of buy-side or sell-side liquidity, a break of "
        "structure the other way, then enter on the retrace into the imbalance (FVG) left "
        "by the displacement.",
        "entry_rules": "- Mark the liquidity: previous day high/low, session highs/lows, "
        "equal highs/lows\n"
        "- Wait for price to sweep that liquidity\n"
        "- Confirm a break of structure (BOS / MSS) with displacement in the opposite "
        "direction\n"
        "- Enter on the retrace into the fair value gap (imbalance), in discount for longs "
        "or premium for shorts\n"
        "- Prefer the London or New York AM killzone",
        "exit_rules": "- Stop beyond the sweep extreme\n"
        "- Target the opposing liquidity pool (draw on liquidity)\n"
        "- Take partials at 2R, trail behind new structure",
        "checklist": [
            "Liquidity swept (PDH/PDL, session high/low, equal highs/lows)",
            "Break of structure with displacement",
            "Fair value gap / imbalance left behind",
            "Entry in discount (longs) or premium (shorts)",
            "Entry inside a killzone",
            "Stop beyond the sweep extreme",
        ],
    },
]

PLANS = [
    "TSLA gapping up pre-market on delivery numbers. Watch for a liquidity sweep of the "
    "pre-market high, then BOS for a short. Max 3 trades.",
    "CPI at 8:30. No trades until 9:45. ES and MNQ: look for 0.618 pullbacks in the trend.",
    "EURUSD at a fresh 4H demand zone. Wait for a London killzone reaction.",
    "Gold (XAUUSD) ranging under supply. Only short from the zone, stop above the distal line.",
    "BTC swept last week's low overnight. Looking for BOS + FVG entry on the 15m.",
    "Choppy, low-volume session expected. Half size, A+ setups only.",
]
REVIEWS_GOOD = [
    "Followed the plan, waited for confirmation at the level and let the runner work.",
    "Good patience on the sweep — entered in the FVG instead of chasing the BOS candle.",
    "Stuck to A+ setups only. Happy with execution today.",
]
REVIEWS_BAD = [
    "Chased TSLA after the first move instead of waiting for the retracement.",
    "Moved my stop on the second trade — that turned a 1R loss into 2.5R.",
    "Traded a zone that had already been tested twice. Overtraded after an early loss.",
]

# Typical stop distance as a fraction of price, by asset class (demo data only).
STOP_DISTANCE = {
    AssetClass.STOCK: 0.009,
    AssetClass.FUTURE: 0.0025,
    AssetClass.COMMODITY: 0.0035,
    AssetClass.FOREX: 0.0017,
    AssetClass.CRYPTO: 0.008,
}


def _reset(engine: Engine) -> None:
    Base.metadata.drop_all(engine)


def seed(engine: Engine | None = None, *, reset: bool = False, rng_seed: int = 7) -> int:
    """Seed the database. Returns the number of trades created."""
    engine = engine or get_engine()
    if reset:
        _reset(engine)
    init_db(engine)
    rng = random.Random(rng_seed)

    with Session(engine) as session:
        if session.scalar(select(func.count(Trade.id))):
            raise SystemExit(
                "Database already has trades. Use --reset to wipe it and re-seed the sample data."
            )
        account = session.scalars(select(Account).order_by(Account.id)).first()
        assert account is not None
        account.name = "Demo (USD)"

        df = pd.read_csv(SAMPLE_CSV, dtype=str, keep_default_na=False)
        mapping = guess_mapping(df.columns)
        save_preset(session, "Sample broker (Ticker/Direction/Open Time)", mapping)
        import_dataframe(session, account.id, df, mapping)

        tags = {(t.category, t.name): t for t in session.scalars(select(Tag))}
        playbooks = _create_playbooks(session)
        trades = session.scalars(select(Trade).order_by(Trade.entry_time)).all()

        for trade in trades:
            _seed_trade(rng, trade, tags, playbooks)

        _seed_journal(session, rng, trades)
        session.commit()
        return len(trades)


def _create_playbooks(session: Session) -> dict[str, Playbook]:
    """Create one playbook per setup; the playbook name equals the setup tag name."""
    result: dict[str, Playbook] = {}
    for spec in PLAYBOOKS:
        pb = Playbook(
            name=spec["name"],
            description=spec["description"],
            entry_rules=spec["entry_rules"],
            exit_rules=spec["exit_rules"],
        )
        pb.checklist_items = [
            PlaybookChecklistItem(text=text, position=i) for i, text in enumerate(spec["checklist"])
        ]
        session.add(pb)
        result[spec["name"]] = pb
    session.flush()
    return result


def _seed_trade(
    rng: random.Random,
    trade: Trade,
    tags: dict[tuple[TagCategory, str], Tag],
    playbooks: dict[str, Playbook],
) -> None:
    is_win = trade.net_pnl > 0
    swing = (
        trade.exit_time is not None and (trade.exit_time.date() - trade.entry_time.date()).days > 0
    )

    # Stop loss on most trades so R-multiples are available.
    if rng.random() < 0.85:
        risk = STOP_DISTANCE[trade.asset_class] * rng.uniform(0.7, 1.4) * (2.5 if swing else 1)
        sign = -1 if trade.side is TradeSide.LONG else 1
        decimals = 5 if trade.avg_entry_price < 10 else 3 if trade.avg_entry_price < 200 else 2
        trade.stop_loss = round(trade.avg_entry_price * (1 + sign * risk), decimals)

    # One setup per trade; the ICT model has the best edge in the demo data.
    setup_names = list(playbooks)
    weights = [3, 2, 4] if is_win else [3, 4, 2]
    setup = rng.choices(setup_names, weights=weights)[0]
    trade.tags.append(tags[(TagCategory.SETUP, setup)])

    # Mistakes mostly on losing trades.
    if trade.status is TradeStatus.CLOSED and rng.random() < (0.15 if is_win else 0.55):
        mistake_names = ["FOMO entry", "Moved stop", "Oversized", "Early exit", "No plan"]
        mistake_weights = [1, 1, 1, 4, 1] if is_win else [4, 3, 2, 1, 2]
        mistake = rng.choices(mistake_names, weights=mistake_weights)[0]
        trade.tags.append(tags[(TagCategory.MISTAKE, mistake)])

    if rng.random() < 0.6:
        emotions = ["Confident", "Calm", "Anxious", "Greedy", "Frustrated"]
        emotion_weights = [4, 4, 1, 1, 1] if is_win else [1, 2, 3, 2, 3]
        trade.tags.append(tags[(TagCategory.EMOTION, rng.choices(emotions, emotion_weights)[0])])

    # Link most trades to the matching playbook and record the checklist.
    if rng.random() < 0.85:
        playbook = playbooks[setup]
        trade.playbook = playbook
        p_met = 0.85 if is_win else 0.55
        trade.checklist_results = [
            TradeChecklistResult(item_id=item.id, met=rng.random() < p_met)
            for item in playbook.checklist_items
        ]

    if rng.random() < 0.25:
        trade.notes = (
            "Clean execution, followed the plan."
            if is_win
            else "Entered before confirmation. **Wait for the candle close** next time."
        )


def _seed_journal(session: Session, rng: random.Random, trades: list[Trade]) -> None:
    daily: dict[date, float] = defaultdict(float)
    for trade in trades:
        if trade.exit_time is not None:
            daily[trade.exit_time.date()] += trade.net_pnl
    for day, pnl in sorted(daily.items()):
        if rng.random() > 0.7:
            continue
        good = pnl > 0
        session.add(
            JournalEntry(
                day=day,
                pre_market_plan=rng.choice(PLANS),
                post_market_review=rng.choice(REVIEWS_GOOD if good else REVIEWS_BAD),
                mood=rng.choice([4, 5]) if good else rng.choice([1, 2, 3]),
                notes="" if rng.random() < 0.6 else "Slept well, no distractions.",
            )
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Load the sample trades into the journal.")
    parser.add_argument("--reset", action="store_true", help="wipe the database first")
    args = parser.parse_args(argv)
    count = seed(reset=args.reset)
    print(f"Seeded {count} trades from {Path(SAMPLE_CSV).name}")


if __name__ == "__main__":
    main()
