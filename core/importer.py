"""CSV import, column mapping, duplicate detection and execution grouping.

Import pipeline
---------------
1. ``rows_to_executions`` turns CSV rows into executions (fills) using a column
   mapping. A row that has both entry and exit columns filled is a round trip and
   becomes two executions (open + close); a row without exit values is a single
   execution.
2. Every execution gets a fingerprint (``import_hash``); fingerprints that already
   exist in the database are skipped, so re-importing a file is a no-op.
3. ``group_executions`` walks the fills of each symbol in time order, tracking the
   net position. A trade starts when the position leaves zero and ends when it
   returns to zero; all partial fills in between belong to the same trade. A fill
   that flips the position (e.g. sell 150 while long 100) is split in two.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.instruments import (
    AssetClass,
    InstrumentSpec,
    default_specs,
    has_broker_suffix,
    normalise_symbol,
    point_value,
    resolve,
)
from core.models import (
    BrokerPreset,
    Execution,
    ExecutionSide,
    Instrument,
    Trade,
    TradeSide,
    TradeStatus,
)

QTY_EPSILON = 1e-9

# Canonical fields a CSV column can be mapped to.
REQUIRED_FIELDS: tuple[str, ...] = ("symbol", "side", "quantity", "entry_price", "entry_time")
OPTIONAL_FIELDS: tuple[str, ...] = ("exit_price", "exit_time", "fees")
ALL_FIELDS: tuple[str, ...] = REQUIRED_FIELDS + OPTIONAL_FIELDS

FIELD_LABELS: dict[str, str] = {
    "symbol": "Symbol",
    "side": "Side (buy/sell or long/short)",
    "quantity": "Quantity",
    "entry_price": "Entry price (or fill price)",
    "entry_time": "Entry time (or fill time)",
    "exit_price": "Exit price",
    "exit_time": "Exit time",
    "fees": "Fees / commission",
}

# Header names we recognise when guessing a mapping (compared lower-case, no spaces).
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "ticker", "instrument", "stock", "code", "contract", "security"),
    "side": ("side", "direction", "action", "buy/sell", "type", "position"),
    "quantity": ("quantity", "qty", "shares", "size", "volume", "units", "lots"),
    "entry_price": ("entryprice", "openprice", "price", "fillprice", "avgprice", "buyprice"),
    "entry_time": (
        "entrytime",
        "opentime",
        "entrydate",
        "opendate",
        "time",
        "datetime",
        "date",
        "filltime",
        "executiontime",
    ),
    "exit_price": ("exitprice", "closeprice", "sellprice"),
    "exit_time": ("exittime", "closetime", "exitdate", "closedate"),
    "fees": ("fees", "fee", "commission", "commissions", "comm", "charges", "brokerage"),
}

_BUY_WORDS = {"buy", "b", "bot", "bought", "long", "l", "bto", "btc", "buy to cover", "cover"}
_SELL_WORDS = {"sell", "s", "sld", "sold", "short", "sh", "ss", "sell short", "sto", "stc"}


class ImportError_(ValueError):
    """Raised when CSV rows cannot be interpreted with the given mapping."""


@dataclass(frozen=True)
class Fill:
    """A single execution, independent of the database."""

    symbol: str
    side: ExecutionSide
    quantity: float
    price: float
    timestamp: datetime
    fees: float = 0.0
    import_hash: str = ""
    # Symbol as it appeared in the source (e.g. "XAUUSD.R"); empty means same as symbol.
    raw_symbol: str = ""

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side is ExecutionSide.BUY else -self.quantity


@dataclass
class TradeGroup:
    """Fills that together form one trade (flat -> position -> flat)."""

    symbol: str
    fills: list[Fill] = field(default_factory=list)
    is_closed: bool = False

    @property
    def side(self) -> TradeSide:
        return TradeSide.LONG if self.fills[0].side is ExecutionSide.BUY else TradeSide.SHORT

    @property
    def entry_side(self) -> ExecutionSide:
        return ExecutionSide.BUY if self.side is TradeSide.LONG else ExecutionSide.SELL

    def summary(self, spec: InstrumentSpec | None = None) -> TradeSummary:
        return summarize_fills(self.fills, self.side, self.is_closed, spec)


@dataclass(frozen=True)
class TradeSummary:
    side: TradeSide
    status: TradeStatus
    quantity: float
    avg_entry_price: float
    avg_exit_price: float | None
    entry_time: datetime
    exit_time: datetime | None
    fees: float
    gross_pnl: float
    net_pnl: float
    instrument: str = ""
    asset_class: AssetClass = AssetClass.STOCK
    point_value: float = 1.0


@dataclass
class ImportResult:
    rows_read: int = 0
    executions_imported: int = 0
    duplicates_skipped: int = 0
    trades_created: int = 0
    trades_updated: int = 0
    errors: list[str] = field(default_factory=list)
    trade_ids: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _normalise_header(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum() or ch == "/")


def guess_mapping(columns: Iterable[str]) -> dict[str, str]:
    """Guess which CSV column holds which field, based on common header names."""
    columns = list(columns)
    normalised = {_normalise_header(c): c for c in columns}
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for fld in ALL_FIELDS:
        for alias in _FIELD_ALIASES[fld]:
            col = normalised.get(alias)
            if col is not None and col not in used:
                mapping[fld] = col
                used.add(col)
                break
    return mapping


def parse_side(value: Any) -> ExecutionSide:
    """Normalise broker side strings (BUY, Sell, long, SS, BTO, ...) to buy/sell."""
    text = str(value).strip().lower()
    if text in _BUY_WORDS:
        return ExecutionSide.BUY
    if text in _SELL_WORDS:
        return ExecutionSide.SELL
    raise ImportError_(f"Unrecognised side value: {value!r}")


def _opposite(side: ExecutionSide) -> ExecutionSide:
    return ExecutionSide.SELL if side is ExecutionSide.BUY else ExecutionSide.BUY


def _parse_number(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    if text == "" or text.lower() in {"nan", "none", "null", "-"}:
        return None
    if text.startswith("(") and text.endswith(")"):  # accounting negative
        text = "-" + text[1:-1]
    return float(text)


def _parse_time(value: Any, fmt: str | None) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    ts = pd.to_datetime(value, format=fmt) if fmt else pd.to_datetime(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.to_pydatetime()


def validate_mapping(mapping: Mapping[str, str | None], columns: Iterable[str]) -> list[str]:
    """Return a list of problems with the mapping (empty if it is usable)."""
    columns = set(columns)
    problems = [
        f"'{FIELD_LABELS[f]}' must be mapped to a column"
        for f in REQUIRED_FIELDS
        if not mapping.get(f)
    ]
    for fld, col in mapping.items():
        if col and col not in columns:
            problems.append(f"Column {col!r} (mapped to {fld}) is not in the CSV")
    has_exit_price = bool(mapping.get("exit_price"))
    has_exit_time = bool(mapping.get("exit_time"))
    if has_exit_price != has_exit_time:
        problems.append("Map both exit price and exit time, or neither")
    return problems


def fingerprint(account_id: int, fill: Fill, occurrence: int) -> str:
    """Stable hash of an execution. ``occurrence`` disambiguates identical rows in a file."""
    key = "|".join(
        [
            str(account_id),
            fill.symbol,
            fill.side.value,
            f"{fill.quantity:.8f}",
            f"{fill.price:.8f}",
            fill.timestamp.isoformat(),
            f"{fill.fees:.8f}",
            str(occurrence),
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()


def assign_fingerprints(account_id: int, fills: Sequence[Fill]) -> list[Fill]:
    """Attach import hashes; identical fills in the same batch get increasing occurrences."""
    seen: dict[tuple[Any, ...], int] = defaultdict(int)
    result: list[Fill] = []
    for f in fills:
        key = (f.symbol, f.side, f.quantity, f.price, f.timestamp, f.fees)
        occurrence = seen[key]
        seen[key] += 1
        result.append(replace(f, import_hash=fingerprint(account_id, f, occurrence)))
    return result


def rows_to_executions(
    df: pd.DataFrame,
    mapping: Mapping[str, str | None],
    datetime_format: str | None = None,
) -> tuple[list[Fill], list[str]]:
    """Convert CSV rows to fills. Returns ``(fills, errors)``; bad rows are reported, not raised."""
    problems = validate_mapping(mapping, df.columns)
    if problems:
        raise ImportError_("; ".join(problems))

    fills: list[Fill] = []
    errors: list[str] = []
    fmt = datetime_format or None

    def col(row: pd.Series, fld: str) -> Any:
        name = mapping.get(fld)
        return row[name] if name else None

    for pos, (_, row) in enumerate(df.iterrows()):
        line = pos + 2  # header is line 1
        try:
            # Kept as written; import_fills normalises it (XAUUSD.R → XAUUSD).
            symbol = str(col(row, "symbol")).strip().upper()
            if not symbol or symbol == "NAN":
                raise ImportError_("missing symbol")
            side = parse_side(col(row, "side"))
            qty = _parse_number(col(row, "quantity"))
            if qty is None or qty == 0:
                raise ImportError_("missing quantity")
            if qty < 0:  # some brokers sign quantities instead of using a side column
                qty = abs(qty)
            entry_price = _parse_number(col(row, "entry_price"))
            entry_time = _parse_time(col(row, "entry_time"), fmt)
            if entry_price is None or entry_time is None:
                raise ImportError_("missing entry price or time")
            fees = _parse_number(col(row, "fees")) or 0.0
            fees = abs(fees)
            exit_price = _parse_number(col(row, "exit_price"))
            exit_time = _parse_time(col(row, "exit_time"), fmt)
            if (exit_price is None) != (exit_time is None):
                raise ImportError_("exit price and exit time must both be set or both empty")

            if exit_price is not None and exit_time is not None:
                if exit_time < entry_time:
                    raise ImportError_("exit time is before entry time")
                half = fees / 2
                fills.append(Fill(symbol, side, qty, entry_price, entry_time, half))
                fills.append(Fill(symbol, _opposite(side), qty, exit_price, exit_time, fees - half))
            else:
                fills.append(Fill(symbol, side, qty, entry_price, entry_time, fees))
        except (ImportError_, ValueError, TypeError) as exc:
            errors.append(f"Row {line}: {exc}")
    return fills, errors


# ---------------------------------------------------------------------------
# Grouping (pure)
# ---------------------------------------------------------------------------


def group_executions(fills: Iterable[Fill]) -> list[TradeGroup]:
    """Group fills into trades by tracking net position per symbol.

    Fills are processed in time order (stable for equal timestamps). The returned
    groups are ordered by their first fill time. The last group of a symbol is open
    (``is_closed=False``) if its position never returned to flat.
    """
    by_symbol: dict[str, list[Fill]] = defaultdict(list)
    for f in fills:
        by_symbol[f.symbol].append(f)

    groups: list[TradeGroup] = []
    for symbol, symbol_fills in by_symbol.items():
        position = 0.0
        current: TradeGroup | None = None
        for fill in sorted(symbol_fills, key=lambda f: f.timestamp):
            remaining = fill
            while remaining is not None:
                if current is None:
                    current = TradeGroup(symbol=symbol)
                    groups.append(current)
                new_position = position + remaining.signed_quantity
                crosses_zero = position != 0 and (position > 0) != (new_position > 0)
                if crosses_zero and abs(new_position) > QTY_EPSILON:
                    # Split: close the existing position, carry the remainder into a new trade.
                    closing_qty = abs(position)
                    ratio = closing_qty / remaining.quantity
                    closing = replace(remaining, quantity=closing_qty, fees=remaining.fees * ratio)
                    rest = replace(
                        remaining,
                        quantity=remaining.quantity - closing_qty,
                        fees=remaining.fees - closing.fees,
                    )
                    current.fills.append(closing)
                    current.is_closed = True
                    current = None
                    position = 0.0
                    remaining = rest
                    continue
                current.fills.append(remaining)
                position = new_position
                if abs(position) <= QTY_EPSILON:
                    position = 0.0
                    current.is_closed = True
                    current = None
                remaining = None
    groups.sort(key=lambda g: (g.fills[0].timestamp, g.symbol))
    return groups


def summarize_fills(
    fills: Sequence[Fill],
    side: TradeSide,
    is_closed: bool,
    spec: InstrumentSpec | None = None,
) -> TradeSummary:
    """Compute averages and P&L (in USD) for a group of fills.

    * quantity: total quantity opened (sum of entry-side fills)
    * gross P&L: (avg exit − avg entry) × closed quantity × point value, sign-adjusted
      for shorts. The point value comes from the instrument spec (contract
      multiplier, forex lot size, USD conversion); it is 1 when no spec is given.
    * net P&L: gross P&L minus all fees
    """
    entry_side = ExecutionSide.BUY if side is TradeSide.LONG else ExecutionSide.SELL
    entries = [f for f in fills if f.side is entry_side]
    exits = [f for f in fills if f.side is not entry_side]
    entry_qty = sum(f.quantity for f in entries)
    exit_qty = sum(f.quantity for f in exits)
    avg_entry = sum(f.quantity * f.price for f in entries) / entry_qty
    avg_exit = sum(f.quantity * f.price for f in exits) / exit_qty if exit_qty else None
    direction = 1.0 if side is TradeSide.LONG else -1.0
    spec = spec or InstrumentSpec(fills[0].symbol, AssetClass.STOCK)
    pv = point_value(spec, avg_exit if avg_exit is not None else avg_entry)
    gross = (avg_exit - avg_entry) * exit_qty * direction * pv if avg_exit is not None else 0.0
    fees = sum(f.fees for f in fills)
    return TradeSummary(
        side=side,
        status=TradeStatus.CLOSED if is_closed else TradeStatus.OPEN,
        quantity=entry_qty,
        avg_entry_price=avg_entry,
        avg_exit_price=avg_exit,
        entry_time=min(f.timestamp for f in fills),
        exit_time=max(f.timestamp for f in exits) if is_closed and exits else None,
        fees=fees,
        gross_pnl=round(gross, 10),
        net_pnl=round(gross - fees, 10),
        instrument=spec.symbol,
        asset_class=spec.asset_class,
        point_value=pv,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _apply_summary(trade: Trade, summary: TradeSummary) -> None:
    trade.side = summary.side
    trade.status = summary.status
    trade.quantity = summary.quantity
    trade.avg_entry_price = summary.avg_entry_price
    trade.avg_exit_price = summary.avg_exit_price
    trade.entry_time = summary.entry_time
    trade.exit_time = summary.exit_time
    trade.fees = summary.fees
    trade.gross_pnl = summary.gross_pnl
    trade.net_pnl = summary.net_pnl
    trade.instrument = summary.instrument
    trade.asset_class = summary.asset_class
    trade.point_value = summary.point_value


def load_specs(session: Session) -> dict[str, InstrumentSpec]:
    """Instrument specs from the database (the built-in catalog if the table is empty)."""
    specs = {
        row.symbol: InstrumentSpec(
            row.symbol, row.asset_class, row.multiplier, row.quote_currency, row.name
        )
        for row in session.scalars(select(Instrument))
    }
    return specs or default_specs()


def replace_instruments(session: Session, specs: Sequence[InstrumentSpec]) -> None:
    """Replace the instrument table with ``specs`` (symbols normalised, must be unique)."""
    cleaned: dict[str, InstrumentSpec] = {}
    for spec in specs:
        symbol = normalise_symbol(spec.symbol)
        if not symbol:
            raise ValueError("Instrument symbol is empty")
        if symbol in cleaned:
            raise ValueError(f"Duplicate instrument {symbol!r}")
        if spec.multiplier <= 0:
            raise ValueError(f"Multiplier for {symbol} must be positive")
        cleaned[symbol] = replace(spec, symbol=symbol, quote_currency=spec.quote_currency.upper())
    for row in session.scalars(select(Instrument)):
        session.delete(row)
    session.flush()
    session.add_all(
        Instrument(
            symbol=s.symbol,
            name=s.name,
            asset_class=s.asset_class,
            multiplier=s.multiplier,
            quote_currency=s.quote_currency,
        )
        for s in cleaned.values()
    )
    session.flush()


def recalculate_trades(session: Session) -> int:
    """Recompute every trade's P&L from its executions with the current instrument specs.

    Use after changing a multiplier. Returns the number of trades updated.
    """
    specs = load_specs(session)
    trades = session.scalars(select(Trade)).all()
    for trade in trades:
        fills = [_fill_from_execution(e) for e in trade.executions]
        if not fills:
            continue
        is_closed = trade.status is TradeStatus.CLOSED
        spec = spec_for(trade.symbol, fills, specs)
        _apply_summary(trade, summarize_fills(fills, trade.side, is_closed, spec))
    session.flush()
    return len(trades)


def _fill_from_execution(e: Execution) -> Fill:
    return Fill(
        e.symbol,
        e.side,
        e.quantity,
        e.price,
        e.timestamp,
        e.fees,
        e.import_hash,
        e.raw_symbol or e.symbol,
    )


def spec_for(
    symbol: str, fills: Sequence[Fill], specs: dict[str, InstrumentSpec]
) -> InstrumentSpec:
    """Instrument spec for a trade. Trades executed with a broker-suffixed symbol
    (``XAUUSD.R``, ``EURUSD.R``) are classed as forex; contract size is unchanged."""
    spec = resolve(symbol, specs)
    if any(has_broker_suffix(f.raw_symbol or f.symbol, specs) for f in fills):
        spec = replace(spec, asset_class=AssetClass.FOREX)
    return spec


def import_fills(
    session: Session,
    account_id: int,
    fills: Sequence[Fill],
    source: str = "csv",
) -> ImportResult:
    """Persist fills: skip duplicates, then group the new fills into trades.

    New fills for a symbol that has an open trade are combined with that trade's
    existing executions, so a position opened in one import and closed in a later
    one becomes a single trade.
    """
    result = ImportResult()
    specs = load_specs(session)
    fills = [
        replace(f, raw_symbol=f.raw_symbol or f.symbol, symbol=normalise_symbol(f.symbol, specs))
        for f in fills
    ]
    fills = assign_fingerprints(account_id, fills)
    hashes = [f.import_hash for f in fills]
    existing: set[str] = set()
    for i in range(0, len(hashes), 500):
        chunk = hashes[i : i + 500]
        existing.update(
            session.scalars(
                select(Execution.import_hash).where(
                    Execution.account_id == account_id, Execution.import_hash.in_(chunk)
                )
            )
        )
    new_fills = [f for f in fills if f.import_hash not in existing]
    result.duplicates_skipped = len(fills) - len(new_fills)
    result.executions_imported = len(new_fills)
    if not new_fills:
        return result

    by_symbol: dict[str, list[Fill]] = defaultdict(list)
    for f in new_fills:
        by_symbol[f.symbol].append(f)

    for symbol, sym_fills in by_symbol.items():
        open_trade = session.scalars(
            select(Trade)
            .where(
                Trade.account_id == account_id,
                Trade.symbol == symbol,
                Trade.status == TradeStatus.OPEN,
            )
            .order_by(Trade.entry_time.desc())
        ).first()
        prior: list[Fill] = []
        if open_trade is not None:
            prior = [_fill_from_execution(e) for e in open_trade.executions]
            # Drop the old execution rows; they are re-created below in grouped form.
            for e in list(open_trade.executions):
                session.delete(e)
            session.flush()

        for i, group in enumerate(group_executions(prior + sym_fills)):
            summary = group.summary(spec_for(symbol, group.fills, specs))
            if i == 0 and open_trade is not None:
                trade = open_trade
                result.trades_updated += 1
            else:
                trade = Trade(account_id=account_id, symbol=symbol)
                session.add(trade)
                result.trades_created += 1
            _apply_summary(trade, summary)
            session.flush()
            result.trade_ids.append(trade.id)
            for f in group.fills:
                session.add(
                    Execution(
                        account_id=account_id,
                        trade_id=trade.id,
                        symbol=f.symbol,
                        raw_symbol=f.raw_symbol or f.symbol,
                        side=f.side,
                        quantity=f.quantity,
                        price=f.price,
                        timestamp=f.timestamp,
                        fees=f.fees,
                        import_hash=f.import_hash,
                        source=source,
                    )
                )
    session.flush()
    session.expire_all()
    return result


def import_dataframe(
    session: Session,
    account_id: int,
    df: pd.DataFrame,
    mapping: Mapping[str, str | None],
    datetime_format: str | None = None,
) -> ImportResult:
    """Parse a CSV DataFrame with a mapping and import it into an account."""
    fills, errors = rows_to_executions(df, mapping, datetime_format)
    result = import_fills(session, account_id, fills, source="csv")
    result.rows_read = len(df)
    result.errors = errors
    return result


def add_manual_trade(
    session: Session,
    account_id: int,
    *,
    symbol: str,
    side: TradeSide,
    quantity: float,
    entry_price: float,
    entry_time: datetime,
    exit_price: float | None = None,
    exit_time: datetime | None = None,
    fees: float = 0.0,
    stop_loss: float | None = None,
    notes: str = "",
) -> Trade | None:
    """Create a trade from the manual entry form. Returns the trade (None if duplicate)."""
    if quantity <= 0:
        raise ImportError_("Quantity must be positive")
    if (exit_price is None) != (exit_time is None):
        raise ImportError_("Provide both exit price and exit time, or neither")
    if exit_time is not None and exit_time < entry_time:
        raise ImportError_("Exit time is before entry time")
    symbol = symbol.strip().upper()
    if not symbol:
        raise ImportError_("Symbol is required")
    entry_side = ExecutionSide.BUY if side is TradeSide.LONG else ExecutionSide.SELL
    fills = []
    if exit_price is not None and exit_time is not None:
        half = fees / 2
        fills = [
            Fill(symbol, entry_side, quantity, entry_price, entry_time, half),
            Fill(symbol, _opposite(entry_side), quantity, exit_price, exit_time, fees - half),
        ]
    else:
        fills = [Fill(symbol, entry_side, quantity, entry_price, entry_time, fees)]
    result = import_fills(session, account_id, fills, source="manual")
    if result.executions_imported == 0:
        return None
    trade = session.get(Trade, result.trade_ids[-1])
    if trade is not None:
        if stop_loss is not None:
            trade.stop_loss = stop_loss
        if notes:
            trade.notes = notes
        session.flush()
    return trade


def save_preset(
    session: Session,
    name: str,
    mapping: Mapping[str, str | None],
    datetime_format: str | None = None,
) -> BrokerPreset:
    """Create or overwrite a broker preset."""
    clean = {k: v for k, v in mapping.items() if v}
    preset = session.scalars(select(BrokerPreset).where(BrokerPreset.name == name)).first()
    if preset is None:
        preset = BrokerPreset(name=name)
        session.add(preset)
    preset.mapping = clean
    preset.datetime_format = datetime_format or None
    session.flush()
    return preset
