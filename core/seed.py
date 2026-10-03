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
        "name": "Opening Range Breakout",
        "description": "Trade the break of the first 15-minute range on strong relative volume.",
        "entry_rules": "- Mark the 9:00–9:15 high/low\n- Enter on a 5-min close outside the range"
        "\n- Volume above 1.5× average",
        "exit_rules": "- Stop on the other side of the range midpoint\n- Scale out at 1R and 2R"
        "\n- Exit remainder by 12:30",
        "checklist": [
            "Clear 15-min opening range",
            "Relative volume > 1.5×",
            "Sector moving in the same direction",
            "Stop placed before entry",
        ],
        "setups": {"Breakout", "Gap and Go"},
    },
    {
        "name": "Pullback to VWAP",
        "description": "Join an established intraday trend on a pullback to VWAP.",
        "entry_rules": "- Price trending above (long) / below (short) VWAP\n- Enter on the first "
        "rejection candle at VWAP",
        "exit_rules": "- Stop beyond the rejection candle\n- Target prior high/low of day",
        "checklist": [
            "Trend established for 30+ minutes",
            "Pullback on lighter volume",
            "Rejection candle at VWAP",
            "Risk under 1% of account",
        ],
        "setups": {"Pullback", "VWAP Bounce"},
    },
    {
        "name": "Failed Breakdown Reversal",
        "description": "Fade a failed break of a key level when sellers/buyers get trapped.",
        "entry_rules": "- Key daily level breaks then reclaims within 15 minutes\n- Enter on "
        "reclaim with volume",
        "exit_rules": "- Stop under the failed-break low\n- Target the opposite side of the range",
        "checklist": [
            "Key daily level identified pre-market",
            "Reclaim within 15 minutes",
            "Volume spike on reclaim",
        ],
        "setups": {"Reversal"},
    },
]

PLANS = [
    "Focus on banks — watch MAYBANK and CIMB for opening range breaks. Max 3 trades.",
    "Choppy futures overnight. Only A+ setups, half size until 10:30.",
    "Tech names gapping up on US semis strength. Watch INARI for gap-and-go.",
    "Utilities strong into results. Look for VWAP pullbacks in TENAGA / YTLPOWR.",
    "No news catalysts. Patience — wait for the first 15-minute range to form.",
]
REVIEWS_GOOD = [
    "Followed the plan, sized correctly and let winners work.",
    "Good patience waiting for confirmation. Scaled out well.",
    "Stuck to A+ setups only. Happy with execution today.",
]
REVIEWS_BAD = [
    "Chased the first move and got stopped. Need to wait for the range.",
    "Moved my stop on the second trade — that turned a small loss into a big one.",
    "Overtraded in the afternoon session after an early loss.",
]


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
        account.name = "Bursa Demo"

        df = pd.read_csv(SAMPLE_CSV)
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


def _create_playbooks(session: Session) -> list[tuple[Playbook, set[str]]]:
    result = []
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
        result.append((pb, set(spec["setups"])))
    session.flush()
    return result


def _seed_trade(
    rng: random.Random,
    trade: Trade,
    tags: dict[tuple[TagCategory, str], Tag],
    playbooks: list[tuple[Playbook, set[str]]],
) -> None:
    is_win = trade.net_pnl > 0
    swing = (
        trade.exit_time is not None and (trade.exit_time.date() - trade.entry_time.date()).days > 0
    )

    # Stop loss on most trades so R-multiples are available.
    if rng.random() < 0.85:
        risk = rng.uniform(0.008, 0.02) if not swing else rng.uniform(0.025, 0.045)
        sign = -1 if trade.side is TradeSide.LONG else 1
        trade.stop_loss = round(trade.avg_entry_price * (1 + sign * risk), 3)

    # One setup per trade; winners lean toward breakouts and pullbacks.
    setup_names = ["Breakout", "Pullback", "Reversal", "Gap and Go", "VWAP Bounce"]
    weights = [4, 4, 1, 2, 3] if is_win else [3, 2, 3, 2, 2]
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
    for playbook, setups in playbooks:
        if setup in setups and rng.random() < 0.8:
            trade.playbook = playbook
            p_met = 0.85 if is_win else 0.55
            trade.checklist_results = [
                TradeChecklistResult(item_id=item.id, met=rng.random() < p_met)
                for item in playbook.checklist_items
            ]
            break

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
