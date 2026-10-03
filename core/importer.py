"""CSV import, column mapping, duplicate detection and execution grouping.

Import pipeline
---------------
1. ``rows_to_executions`` turns CSV rows into executions (fills) using a column
   mapping. A row that has both entry and exit columns filled is a round trip and
   becomes two executions (open + close); a row without exit values is a single
   execution. Order-history exports (one row per order, e.g. Alchemy Markets) are
   filtered to filled orders; stop-loss orders supply the trade's stop.
2. Every execution gets a fingerprint (``import_hash``): the broker order ID when
   one is mapped, otherwise a hash of the row. Known fingerprints are skipped, so
   re-importing a file is a no-op.
3. Fills with a broker position ID are grouped by that ID (one position = one
   trade, which also handles hedging accounts with several positions in the same
   symbol). Other fills are grouped by ``group_executions``, which walks each
   symbol's fills in time order, tracking the net position: a trade starts when the
   position leaves zero and ends when it returns to zero, and a fill that flips the
   position (sell 150 while long 100) is split in two.
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
ROUND_TRIP_FIELDS: tuple[str, ...] = ("exit_price", "exit_time", "fees")
# Columns found in broker order-history exports (one row per order).
ORDER_HISTORY_FIELDS: tuple[str, ...] = (
    "status",
    "order_type",
    "stop_price",
    "position_id",
    "order_id",
    "closed_pnl",
    "net_closed_pnl",
)
OPTIONAL_FIELDS: tuple[str, ...] = ROUND_TRIP_FIELDS + ORDER_HISTORY_FIELDS
ALL_FIELDS: tuple[str, ...] = REQUIRED_FIELDS + OPTIONAL_FIELDS

FIELD_LABELS: dict[str, str] = {
    "symbol": "Symbol",
    "side": "Side (buy/sell or long/short)",
    "quantity": "Quantity (filled)",
    "entry_price": "Entry price (or fill price)",
    "entry_time": "Entry time (or fill time)",
    "exit_price": "Exit price",
    "exit_time": "Exit time",
    "fees": "Fees / commission",
    "status": "Order status",
    "order_type": "Order type",
    "stop_price": "Stop price",
    "position_id": "Position ID",
    "order_id": "Order ID",
    "closed_pnl": "Closed P&L",
    "net_closed_pnl": "Net closed P&L",
}

FIELD_HELP: dict[str, str] = {
    "status": "Only rows with a filled status (Filled, Partially filled, Executed…) are "
    "imported; cancelled, rejected and pending orders are skipped.",
    "order_type": "Stop loss / take profit orders are treated as closing fills.",
    "stop_price": "The stop price of the position's Stop Loss order becomes the trade's "
    "stop loss, so R-multiples are filled in.",
    "position_id": "Fills with the same position ID form one trade (works for hedging "
    "accounts with several positions in the same symbol).",
    "order_id": "Used to skip orders that were already imported.",
    "closed_pnl": "The broker's realised P&L on closing fills (USD). Used as the trade's "
    "gross P&L, which also converts crosses such as EURJPY correctly.",
    "net_closed_pnl": "The broker's realised P&L after commission, including swap and "
    "financing. Used as the trade's net P&L so totals match your statement.",
}

# Header names we recognise when guessing a mapping (compared lower-case, no spaces).
# Earlier aliases win, so "Filled Qty" is preferred over "Qty" and "Avg Fill Price"
# over "Limit Price".
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "ticker", "instrument", "stock", "code", "contract", "security"),
    "side": ("side", "direction", "action", "buy/sell", "type", "position"),
    "quantity": (
        "filledqty",
        "filledquantity",
        "executedqty",
        "execqty",
        "quantity",
        "qty",
        "shares",
        "size",
        "volume",
        "units",
        "lots",
    ),
    "entry_price": (
        "avgfillprice",
        "averagefillprice",
        "fillprice",
        "executionprice",
        "entryprice",
        "openprice",
        "price",
        "avgprice",
        "averageprice",
        "buyprice",
    ),
    "entry_time": (
        "entrytime",
        "opentime",
        "filltime",
        "executiontime",
        "updatetime",
        "entrydate",
        "opendate",
        "time",
        "datetime",
        "date",
    ),
    "exit_price": ("exitprice", "closeprice", "sellprice"),
    "exit_time": ("exittime", "closetime", "exitdate", "closedate"),
    "fees": ("fees", "fee", "commission", "commissions", "comm", "charges", "brokerage"),
    "status": ("status", "orderstatus", "state"),
    "order_type": ("ordertype", "type"),
    "stop_price": ("stopprice", "stoplossprice", "stoploss", "sl"),
    "position_id": ("positionid", "positionticket", "positionno", "positionnumber"),
    "order_id": ("orderid", "orderno", "ordernumber", "ticket", "dealid", "tradeid"),
    "net_closed_pnl": ("netclosedpl", "netclosedpnl", "netrealizedpl", "netrealisedpl"),
    "closed_pnl": (
        "closedpl",
        "closedpnl",
        "realizedpl",
        "realisedpl",
        "realizedpnl",
        "realisedpnl",
    ),
}

# Order statuses that mean the order (at least partly) executed.
FILLED_STATUSES = {
    "filled",
    "partiallyfilled",
    "partfilled",
    "partialfill",
    "executed",
    "done",
    "complete",
    "completed",
    "closed",
    "fill",
    "trade",
    "matched",
}
STOP_LOSS_ORDER_TYPES = {"stoploss", "sl"}
EXIT_ORDER_TYPES = STOP_LOSS_ORDER_TYPES | {"takeprofit", "tp"}

# Built-in broker presets: name -> (mapping, datetime format).
BUILTIN_PRESETS: dict[str, tuple[dict[str, str], str | None]] = {
    "Alchemy Markets – order history": (
        {
            "symbol": "Symbol",
            "side": "Side",
            "quantity": "Filled Qty",
            "entry_price": "Avg Fill Price",
            "entry_time": "Update Time",
            "fees": "Commission",
            "status": "Status",
            "order_type": "Type",
            "stop_price": "Stop Price",
            "position_id": "Position ID",
            "order_id": "Order ID",
            "closed_pnl": "Closed P&L",
            "net_closed_pnl": "Net Closed P&L",
        },
        "%Y-%m-%d %H:%M:%S",
    ),
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
    # Optional broker data from order-history exports.
    order_id: str = ""
    position_id: str = ""
    is_exit: bool = False  # known to close a position (stop loss / take profit / P&L)
    realized_pnl: float | None = None  # broker's realised P&L on a closing fill, USD
    realized_net_pnl: float | None = None  # same after commission, incl. swap
    stop_loss: float | None = None  # stop of the position's stop-loss order

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
    rows_skipped: int = 0  # cancelled / rejected / unfilled orders
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trade_ids: list[int] = field(default_factory=list)


@dataclass
class ParsedRows:
    """Result of reading CSV rows: fills, per-row errors and rows that are not fills."""

    fills: list[Fill] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: int = 0


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _normalise_header(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum() or ch == "/")


def detect_preset(columns: Iterable[str], presets: Mapping[str, Mapping[str, str]]) -> str | None:
    """Name of the preset whose mapped columns are all in the CSV (most columns wins)."""
    cols = set(columns)
    matches = [
        (len(mapping), name)
        for name, mapping in presets.items()
        if mapping and all(c in cols for c in mapping.values() if c)
    ]
    return max(matches)[1] if matches else None


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
    """Stable hash of an execution: the broker order ID when known, otherwise the row's
    values. ``occurrence`` disambiguates identical rows in a file."""
    if fill.order_id:
        return hashlib.sha256(f"{account_id}|order|{fill.order_id}".encode()).hexdigest()
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


def _norm_word(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _clean_id(value: Any) -> str:
    text = "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)
    text = text.strip()
    return "" if text.lower() in {"", "nan", "none", "null"} else text.removesuffix(".0")


def rows_to_executions(
    df: pd.DataFrame,
    mapping: Mapping[str, str | None],
    datetime_format: str | None = None,
) -> ParsedRows:
    """Convert CSV rows to fills. Bad rows are reported in ``errors``, not raised.

    Rows whose order status is not a filled status, or whose quantity is 0, are not
    fills (cancelled or pending orders) and are only counted in ``skipped``.
    """
    problems = validate_mapping(mapping, df.columns)
    if problems:
        raise ImportError_("; ".join(problems))

    parsed = ParsedRows()
    fmt = datetime_format or None
    # (raw symbol, order id, position id, stop price) of stop-loss orders, any status.
    stops: list[tuple[str, str, str, float]] = []

    def col(row: pd.Series, fld: str) -> Any:
        name = mapping.get(fld)
        return row[name] if name else None

    for pos, (_, row) in enumerate(df.iterrows()):
        line = pos + 2  # header is line 1
        try:
            # Kept as written; import_fills normalises it (XAUUSD.R → XAUUSD).
            symbol = str(col(row, "symbol")).strip().upper()
            order_type = _norm_word(col(row, "order_type"))
            order_id = _clean_id(col(row, "order_id"))
            position_id = _clean_id(col(row, "position_id"))
            if order_type in STOP_LOSS_ORDER_TYPES:
                stop_price = _parse_number(col(row, "stop_price"))
                if stop_price:
                    stops.append((symbol, order_id, position_id, stop_price))

            status = _norm_word(col(row, "status"))
            if mapping.get("status") and status and status not in FILLED_STATUSES:
                parsed.skipped += 1
                continue
            qty = _parse_number(col(row, "quantity"))
            if qty == 0:
                parsed.skipped += 1
                continue

            if not symbol or symbol == "NAN":
                raise ImportError_("missing symbol")
            side = parse_side(col(row, "side"))
            if qty is None:
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
            realized = _parse_number(col(row, "closed_pnl"))
            realized_net = _parse_number(col(row, "net_closed_pnl"))

            if exit_price is not None and exit_time is not None:
                if exit_time < entry_time:
                    raise ImportError_("exit time is before entry time")
                half = fees / 2
                parsed.fills.append(Fill(symbol, side, qty, entry_price, entry_time, half))
                parsed.fills.append(
                    Fill(
                        symbol,
                        _opposite(side),
                        qty,
                        exit_price,
                        exit_time,
                        fees - half,
                        is_exit=True,
                        realized_pnl=realized,
                    )
                )
            else:
                parsed.fills.append(
                    Fill(
                        symbol,
                        side,
                        qty,
                        entry_price,
                        entry_time,
                        fees,
                        order_id=order_id,
                        position_id=position_id,
                        is_exit=order_type in EXIT_ORDER_TYPES or realized is not None,
                        realized_pnl=realized,
                        realized_net_pnl=realized_net,
                    )
                )
        except (ImportError_, ValueError, TypeError) as exc:
            parsed.errors.append(f"Row {line}: {exc}")

    if stops:
        parsed.fills = _attach_stop_losses(parsed.fills, stops)
    return parsed


def _attach_stop_losses(fills: list[Fill], stops: list[tuple[str, str, str, float]]) -> list[Fill]:
    """Give opening fills the stop price of their position's stop-loss order.

    A stop-loss order belongs to a position when it has the same position ID, or, for
    bracket orders that were cancelled (and so carry no position ID), when its order
    ID is 1–3 above the opening order's ID on the same symbol.
    """
    by_position = {(sym, pid): price for sym, _, pid, price in stops if pid}
    by_order = {(sym, oid): price for sym, oid, _, price in stops if oid.isdigit()}
    out = []
    for f in fills:
        stop = None
        if not f.is_exit:
            stop = by_position.get((f.symbol, f.position_id)) if f.position_id else None
            if stop is None and f.order_id.isdigit():
                for offset in (1, 2, 3):
                    stop = by_order.get((f.symbol, str(int(f.order_id) + offset)))
                    if stop is not None:
                        break
        out.append(replace(f, stop_loss=stop) if stop is not None else f)
    return out


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
        groups.extend(_walk(symbol, sorted(symbol_fills, key=lambda f: f.timestamp)))
    groups.sort(key=lambda g: (g.fills[0].timestamp, g.symbol))
    return groups


def position_order(fill: Fill) -> tuple[datetime, bool, int]:
    """Sort key within a broker position: time, then openings before closings, then
    order ID (entry orders are created before their stop-loss/take-profit orders)."""
    order = int(fill.order_id) if fill.order_id.isdigit() else 0
    return (fill.timestamp, fill.is_exit, order)


def group_by_position(fills: Iterable[Fill]) -> list[TradeGroup]:
    """Group fills that carry a broker position ID: each position becomes one trade
    (or more, if the position was flattened and reopened under the same ID)."""
    by_key: dict[tuple[str, str], list[Fill]] = defaultdict(list)
    for f in fills:
        by_key[(f.symbol, f.position_id)].append(f)
    groups: list[TradeGroup] = []
    for (symbol, _), pos_fills in by_key.items():
        groups.extend(_walk(symbol, sorted(pos_fills, key=position_order)))
    groups.sort(key=lambda g: (g.fills[0].timestamp, g.symbol))
    return groups


def _walk(symbol: str, ordered: Iterable[Fill]) -> list[TradeGroup]:
    """Split time-ordered fills of one symbol into flat-to-flat trades."""
    groups: list[TradeGroup] = []
    position = 0.0
    current: TradeGroup | None = None
    for fill in ordered:
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
                # The remainder opens a new position: it carries no realised P&L.
                rest = replace(
                    remaining,
                    quantity=remaining.quantity - closing_qty,
                    fees=remaining.fees - closing.fees,
                    is_exit=False,
                    realized_pnl=None,
                    realized_net_pnl=None,
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
    * If every closing fill carries the broker's realised P&L, their sum is used as the
      gross P&L instead (it is already in USD, which matters for crosses like EURJPY).
    * net P&L: gross P&L minus all fees; or, if every closing fill carries the broker's
      net realised P&L, their sum (which includes swap/financing).
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
    if exits and all(f.realized_pnl is not None for f in exits):
        gross = sum(f.realized_pnl or 0.0 for f in exits)
    fees = sum(f.fees for f in fills)
    net = gross - fees
    if exits and all(f.realized_net_pnl is not None for f in exits):
        net = sum(f.realized_net_pnl or 0.0 for f in exits)
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
        net_pnl=round(net, 10),
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
        spec = resolve(trade.symbol, specs)
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
        order_id=e.order_id or "",
        position_id=e.position_id or "",
        is_exit=e.realized_pnl is not None,
        realized_pnl=e.realized_pnl,
        realized_net_pnl=e.realized_net_pnl,
    )


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

    by_key: dict[tuple[str, str], list[Fill]] = defaultdict(list)
    for f in new_fills:
        by_key[(f.symbol, f.position_id)].append(f)

    for (symbol, position_id), key_fills in by_key.items():
        open_trade = _open_trade_for(session, account_id, symbol, position_id)
        prior: list[Fill] = []
        if open_trade is not None:
            prior = [_fill_from_execution(e) for e in open_trade.executions]

        if position_id and not prior and min(key_fills, key=position_order).is_exit:
            # The opening fill is not in this file (the position was opened before the
            # export range). Leave it out; a longer export will bring it in later.
            result.executions_imported -= len(key_fills)
            result.warnings.append(
                f"{key_fills[0].raw_symbol} position {position_id}: closing fill without "
                "its opening fill — export a longer date range to import this trade."
            )
            continue
        if open_trade is not None:
            # Drop the old execution rows; they are re-created below in grouped form.
            for e in list(open_trade.executions):
                session.delete(e)
            session.flush()

        combined = prior + key_fills
        groups = group_by_position(combined) if position_id else group_executions(combined)
        for i, group in enumerate(groups):
            summary = group.summary(resolve(symbol, specs))
            if i == 0 and open_trade is not None:
                trade = open_trade
                result.trades_updated += 1
            else:
                trade = Trade(account_id=account_id, symbol=symbol)
                session.add(trade)
                result.trades_created += 1
            _apply_summary(trade, summary)
            if trade.stop_loss is None:
                trade.stop_loss = initial_stop(summary, group.fills)
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
                        order_id=f.order_id or None,
                        position_id=f.position_id or None,
                        realized_pnl=f.realized_pnl,
                        realized_net_pnl=f.realized_net_pnl,
                        import_hash=f.import_hash,
                        source=source,
                    )
                )
    session.flush()
    session.expire_all()
    return result


def initial_stop(summary: TradeSummary, fills: Sequence[Fill]) -> float | None:
    """Stop loss from the position's stop-loss order, if it can be the initial risk.

    Exports only show a stop order's last price. A stop on the profit side of the
    entry (moved to breakeven or trailed) says nothing about the risk taken, so it is
    ignored and the stop is left for the user to enter.
    """
    stop = next((f.stop_loss for f in fills if f.stop_loss is not None), None)
    if stop is None:
        return None
    entry = summary.avg_entry_price
    on_risk_side = stop < entry if summary.side is TradeSide.LONG else stop > entry
    return stop if on_risk_side else None


def _open_trade_for(
    session: Session, account_id: int, symbol: str, position_id: str
) -> Trade | None:
    """The open trade new fills should continue: the trade holding the same broker
    position, or (without a position ID) the open trade in the symbol that is not
    tracked by position ID."""
    stmt = select(Trade).where(
        Trade.account_id == account_id,
        Trade.symbol == symbol,
        Trade.status == TradeStatus.OPEN,
    )
    if position_id:
        stmt = stmt.where(Trade.executions.any(Execution.position_id == position_id))
    else:
        stmt = stmt.where(~Trade.executions.any(Execution.position_id.is_not(None)))
    return session.scalars(stmt.order_by(Trade.entry_time.desc())).first()


def import_dataframe(
    session: Session,
    account_id: int,
    df: pd.DataFrame,
    mapping: Mapping[str, str | None],
    datetime_format: str | None = None,
) -> ImportResult:
    """Parse a CSV DataFrame with a mapping and import it into an account."""
    parsed = rows_to_executions(df, mapping, datetime_format)
    result = import_fills(session, account_id, parsed.fills, source="csv")
    result.rows_read = len(df)
    result.rows_skipped = parsed.skipped
    result.errors = parsed.errors
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
