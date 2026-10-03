# Trade Journal

A personal trading journal and analytics app inspired by Tradezella, built with
Streamlit, SQLAlchemy and pandas. It covers **US stocks, futures, forex, commodities
and crypto**, with all accounts in **USD**. Import your broker's CSV, tag trades, keep
a daily journal, run your playbooks and see where your edge is.

![Dashboard](docs/screenshots/dashboard.png)

## Features

| Area | What you get |
|---|---|
| **Import** | CSV import with a column-mapping screen and reusable *broker presets*; manual trade entry; partial fills grouped into trades; duplicate rows skipped; multiple USD accounts; contract multipliers for futures, forex lots and spot commodities ([details](#markets-and-instruments)) |
| **Dashboard** | Net P&L, win rate, profit factor, avg win, avg loss, avg win/loss ratio, expectancy, total trades, max drawdown, cumulative P&L curve with drawdown shading, daily P&L bars and a 0–100 *journal score* radar |
| **Calendar** | Monthly calendar with per-day net P&L, trade count and journal mood, colored green/red, with weekly totals. Click a day to see its trades and journal entry |
| **Trade log** | Sortable, filterable table (symbol, side, status, result, tags, playbook) with CSV export. Select a row to open the trade |
| **Trade detail** | Entry/exit, P&L, fees, R-multiple (once a stop loss is set), holding time, markdown notes, screenshot uploads (stored locally), tags, playbook checklist, executions |
| **Tags** | Three setups (**Golden Ratio - 0.618 retracement**, **Supply/Demand zone**, **ICT - Liquidity Sweep + BOS + Imbalance**), plus Mistakes and Emotions. Custom tags can be added; several tags per trade |
| **Daily journal** | One entry per trading day: pre-market plan, post-market review, mood (1–5) and notes |
| **Playbooks** | One playbook per setup, each with a description, entry/exit rules and a checklist (see [Setups and playbooks](#setups-and-playbooks)). Link trades, tick the criteria that were met, and see per-playbook stats and checklist adherence |
| **Reports** | P&L, win rate and trade count by market (asset class), symbol (futures grouped by root), day of week, entry hour, holding time, long vs short, each tag and month |

The **account**, **market** and **date range** filters in the sidebar apply to every page.

## Quick start

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 if you don't have it.

```bash
git clone <this repo> trade-journal && cd trade-journal
uv sync                         # create .venv and install dependencies
uv run trade-journal-seed       # load ~200 sample trades (TSLA, ES, EURUSD, XAUUSD, BTC…), playbooks, journal
uv run streamlit run app/main.py
```

Open http://localhost:8501. To start again from the sample data, run
`uv run trade-journal-seed --reset` (this wipes the database). To start with an empty
journal, skip the seed step; the app creates a default `Main` account (USD).

> **Upgrading from an earlier build?** The schema changed (accounts lost their currency;
> trades gained market and point value). Run `uv run trade-journal-seed --reset`, or
> delete `data/journal.db`, before starting the app.

## Screenshots

| | |
|---|---|
| ![Calendar](docs/screenshots/calendar.png) **Calendar** | ![Day details](docs/screenshots/calendar_day.png) **Clicking a day** |
| ![Trade log](docs/screenshots/trade_log.png) **Trade log** | ![Trade detail](docs/screenshots/trade_detail.png) **Trade detail** |
| ![Reports by market](docs/screenshots/reports_markets.png) **Reports: by market** | ![Reports by tag](docs/screenshots/reports_tags.png) **Reports: setups, mistakes, emotions** |
| ![Playbooks](docs/screenshots/playbooks.png) **Playbooks** | ![Instruments](docs/screenshots/instruments.png) **Settings: instruments and multipliers** |
| ![Import](docs/screenshots/import_mapping.png) **CSV column mapping** | ![Journal](docs/screenshots/journal.png) **Daily journal** |

## CSV format

Any CSV with a header row works. On the **Import** page the app recognises known broker
exports (**Alchemy Markets** order history is built in) and maps the columns for you;
otherwise common header names are detected and you can adjust the mapping and save it as
a broker preset. Before importing, the page previews how many fills, ignored orders and
positions it found.

| Field | Required | Notes |
|---|---|---|
| Symbol | yes | `TSLA`, `ESZ6`, `/MNQH7`, `EURUSD`, `EUR/USD`, `EURUSD.R`, `XAUUSD.R`, `BTCUSD`, `BTC-USDT`… normalised on import (see [Markets and instruments](#markets-and-instruments)) |
| Side | yes | `buy`/`sell`, `long`/`short`, `B`/`S`, `BOT`/`SLD`, `BTO`/`STC`, `SS`… (case-insensitive) |
| Quantity | yes | Shares (stocks), contracts (futures), lots (forex and spot commodities) or coins (crypto). Thousands separators allowed; a negative quantity is treated as its absolute value |
| Entry price | yes | The fill price for one-row-per-execution files |
| Entry time | yes | Auto-detected, or give a `strftime` format such as `%d/%m/%Y %H:%M` |
| Exit price | no | Map together with exit time for round-trip rows |
| Exit time | no | |
| Fees | no | Commission plus other charges. On round-trip rows, half goes to each leg |
| Order status | no | Order-history exports: only filled orders are imported (`Filled`, `Partially filled`, `Executed`…); cancelled, rejected and working orders are ignored. Rows with quantity 0 are always ignored |
| Order type | no | `Stop Loss` / `Take Profit` orders are closing fills |
| Stop price | no | The stop of the position's `Stop Loss` order becomes the trade's stop loss |
| Position ID | no | Fills with the same broker position ID form one trade |
| Order ID | no | Used to skip orders that were already imported |
| Closed P&L | no | Broker's realised P&L (USD) on closing fills, used as gross P&L |
| Net closed P&L | no | Broker's realised P&L after commission and swap, used as net P&L |

Three layouts are supported:

1. **One row per execution (fill).** Map symbol, side, quantity, entry price/time and
   fees; leave exit price/time unmapped.

   ```csv
   Symbol,Action,Qty,Price,Time,Commission
   TSLA,BUY,100,431.20,2026-06-01 09:31:02,1.00
   TSLA,BUY,100,429.80,2026-06-01 09:33:40,1.00
   TSLA,SELL,200,436.15,2026-06-01 10:05:11,1.00
   ```

   These three fills become **one TSLA trade**: long 200 shares, average entry 430.50,
   exit 436.15, net P&L (436.15 − 430.50) × 200 − 3.00 = **+$1,127.00**.

2. **One row per round trip.** Also map exit price and exit time. The side is the
   direction of the position (`Long`/`Buy` opens with a buy). A row with an empty
   exit is an open position.

   ```csv
   Ticker,Direction,Qty,Entry Price,Open Time,Exit Price,Close Time,Commission
   TSLA,Short,150,438.40,2026-06-02 09:45:10,432.90,2026-06-02 10:20:41,2.00
   ESU6,Long,2,6500.25,2026-06-02 10:02:00,6512.75,2026-06-02 11:15:30,9.00
   EURUSD,Long,0.5,1.16520,2026-06-02 03:15:00,1.16810,2026-06-02 06:40:00,3.50
   ```

   That is +$823.00 on TSLA, +$1,241.00 on ES (12.5 points × 2 contracts × $50) and
   +$141.50 on EURUSD (29 pips × 0.5 lot × $10), each after fees.

3. **Broker order history** (one row per order, e.g. Alchemy Markets). Detected
   automatically from the header:

   ```csv
   Symbol,Side,Type,Qty,Filled Qty,Limit Price,Stop Price,Avg Fill Price,Status,Update Time,Position ID,Commission,Closed P&L,Net Closed P&L,Order ID
   XAUUSD.R,Sell,Limit,0.5,0.5,4350,,4350.00,Filled,2026-09-17 01:20:19,XAUUSD.R:3000,-0.75,,,3000
   XAUUSD.R,Buy,Take Profit,0.5,0,4329.3,,,Cancelled,2026-09-17 01:59:55,,0.0,,,3001
   XAUUSD.R,Buy,Stop Loss,0.5,0.5,,4360,4360.20,Filled,2026-09-17 01:20:19,XAUUSD.R:3000,-0.75,-510.00,-511.50,3002
   ```

   Built-in mapping: `Filled Qty` (not `Qty`), `Avg Fill Price`, `Update Time`,
   `Commission`, plus the order-history columns above. The example is **one short
   XAUUSD trade** of 0.5 lot with a stop loss of 4360, net **−$511.50**. The cancelled
   take-profit order is ignored. How the import handles this format:
   * **One trade per position.** Fills are grouped by `Position ID`, so a hedging account
     with two XAUUSD positions open at once gets two trades.
   * **The broker's P&L.** `Closed P&L` / `Net Closed P&L` are used, so totals match your
     statement, including swap and crosses such as EURJPY.
   * **Stop loss from the bracket.** The stop comes from the position's `Stop Loss`
     order, linked by position ID or, for a cancelled bracket, by the order ID right
     after the entry. Only a stop's last price is exported, so a stop that ended on the
     profit side of the entry (moved to breakeven or trailed) is left empty instead of
     producing a misleading R-multiple.
   * **Exits whose entry is missing.** A closing fill whose opening fill falls before the
     export's date range is skipped with a note. Import a longer range and it is added
     then.

`sample_data/sample_trades.csv` uses the second layout. It has 245 rows that group into
207 trades across US stocks (TSLA is the most traded), futures (ES, MES, MNQ), commodities
(CL, GC, XAUUSD), forex (EURUSD, GBPUSD, USDJPY) and crypto (BTCUSD, ETHUSD), because some
positions were scaled out of across several rows. Times are US Eastern. It was made by
`sample_data/generate_sample.py`.

### How trades are built

* **Grouping.** Each row becomes one execution, or two for a round-trip row. Executions
  with a broker position ID are grouped by that ID. The others are walked per symbol in
  time order while tracking the net position.
  A trade starts when the position leaves zero and ends when it returns to zero, so all
  partial fills in between belong to one trade. A fill that flips the position (selling
  150 while long 100) is split: 100 closes the long, 50 opens a new short.
* **Prices and P&L.** Average entry and exit are volume-weighted. Gross P&L is
  (avg exit − avg entry) × closed quantity × point value, sign-adjusted for shorts. Net
  P&L is gross P&L minus all fees. When the export includes the broker's realised P&L,
  that is used instead. All P&L is in USD.
* **Open positions.** A trade that hasn't returned to flat is stored as *open* and
  **left out of every statistic**. If a later import closes it, the executions are
  added to the same trade, so its notes and tags are kept.
* **Duplicates.** Every execution gets a fingerprint: the broker order ID when mapped,
  otherwise the account, symbol, side, quantity, price, time and fees. Fingerprints that are already in the account are skipped, so
  re-importing an overlapping export is safe. Identical rows within one file are told
  apart by how many times they occur, so two genuine identical fills both import.

## Markets and instruments

Every trade is resolved to an instrument, which gives it a **market** (asset class) and a
**point value**: the USD value of a 1.0 price move for one unit of quantity.

| Market | Quantity unit | Point value | Examples |
|---|---|---|---|
| US stocks | shares | 1 | `TSLA`, `NVDA`, `AAPL`; any symbol not otherwise recognised |
| Futures | contracts | contract multiplier | `ES` 50, `MES` 5, `NQ` 20, `MNQ` 2, `YM` 5, `RTY` 50, `ZN` 1,000, `6E` 125,000 |
| Commodities | contracts (futures) or lots (spot) | contract/lot size | `CL` 1,000, `MCL` 100, `GC` 100, `MGC` 10, `SI` 5,000, `NG` 10,000, `XAUUSD` 100 oz, `XAGUSD` 5,000 oz, `USOIL` 1,000 bbl |
| Forex | standard lots | 100,000 | `EURUSD`, `GBPUSD`, `AUDUSD`; `USDJPY`/`USDCAD`/`USDCHF` are converted to USD at the exit price |
| Crypto | coins | 1 | `BTCUSD`, `ETHUSD`, `SOLUSD`; `BTCUSDT`/`BTC-USD` are normalised to `BTCUSD` |

How a symbol is resolved:

0. **Clean-up.** Common spellings are normalised first: `EUR/USD` → `EURUSD`,
   `BTC-USDT` → `BTCUSD`, `/ESZ6` → `ESZ6`, `ES1!` → `ES`. Broker account suffixes after
   a dot are dropped when the rest is a market symbol, so **`XAUUSD.R` is treated as
   `XAUUSD` and `EURUSD.R` as `EURUSD`** (also `.r`, `.m`, `.pro`, …). They get the base
   symbol's market and multiplier: `XAUUSD.R` is a **commodity** (gold, 100 oz per lot)
   and `EURUSD.R` is **forex** (100,000 per lot). Rows with and without the suffix
   belong to the same position. The original symbol is kept on each execution and
   shown on the trade page. Dotted stock tickers such as `BRK.B` are left alone.

1. **Exact match** in the instrument table.
2. **Futures contract code**: root + month code + year (`ESZ6`, `ESZ26`, `MNQH2027`)
   resolves to the root (`ES`, `MNQ`). Each contract is still its own position, and
   reports group contracts by root.
3. **Forex pattern**: two ISO currency codes (`EURGBP`) → forex, 100,000 per lot.
4. **Crypto pattern**: a known coin + `USD`/`USDT`/`USDC` → crypto, 1 per coin.
5. Otherwise a **US stock** with point value 1.

You can edit the table under **Settings → Instruments**. For example, set a forex
multiplier to 1 if your broker exports units instead of lots, or add a contract. Saving
recalculates the P&L of every stored trade from its executions.

## Setups and playbooks

The journal is built around three setups. Each one is a **Setup** tag and has a matching
**playbook** with a description, entry/exit rules and a checklist. Ticking the checklist
on a trade feeds the "followed the checklist?" comparison on the Playbooks page.

| Setup / playbook | Idea | Checklist |
|---|---|---|
| **Golden Ratio - 0.618 retracement** | Buy (sell) the pullback to the 61.8% Fibonacci retracement of a clean impulse leg, with the higher-timeframe trend | HTF trend aligned · clean impulse leg with displacement · price reached the 0.618 / golden pocket · rejection candle at the level · stop beyond 0.786 · R:R ≥ 2 |
| **Supply/Demand zone** | Trade the first return to a fresh zone created by a strong departure (rally-base-drop / drop-base-rally) | fresh, untested zone · strong departure from the base · zone aligned with HTF trend · ≥ 2R to the opposing zone · stop beyond the distal line |
| **ICT - Liquidity Sweep + BOS + Imbalance** | After a sweep of buy-side/sell-side liquidity and a break of structure the other way, enter on the retrace into the fair value gap | liquidity swept (PDH/PDL, session high/low, equal highs/lows) · BOS with displacement · FVG left behind · entry in discount/premium · inside a killzone · stop beyond the sweep extreme |

The full rules are seeded by `uv run trade-journal-seed` and can be edited on the
Playbooks page.

## Metric definitions

All metrics are pure functions in `core/metrics.py`, computed over **closed** trades
in the current filter. Each one has unit tests in `tests/test_metrics.py`.

| Metric | Definition |
|---|---|
| Win / loss | A trade with net P&L > 0 / < 0. Break-even trades count in the total but are neither |
| Net P&L | Sum of net P&L (after fees) |
| Win rate | wins ÷ total closed trades |
| Gross profit / gross loss | Sum of winning trades / sum of losing trades (≤ 0) |
| Profit factor | gross profit ÷ \|gross loss\|. **∞** if there are wins but no losses; undefined (–) with no wins and no losses |
| Average win | Mean net P&L of winning trades |
| Average loss | Mean \|net P&L\| of losing trades (shown as a positive number) |
| Avg win/loss ratio | average win ÷ average loss (∞ with no losses) |
| Expectancy | win rate × average win − loss rate × average loss. This equals the mean net P&L per trade |
| Max drawdown | Largest peak-to-trough fall of cumulative net P&L, starting from equity 0 |
| R-multiple | net P&L ÷ (\|avg entry − stop loss\| × quantity × point value). Only shown once a stop loss is set |
| Holding time | exit time − entry time. Buckets: < 1 min, 1–5 min, 5–15 min, 15–60 min, 1–4 h, 4–24 h, 1–7 days, > 7 days |
| Trade date | The exit date. The calendar, daily P&L and date filter use it (open trades use their entry date) |

With no trades, totals are 0 and ratios are shown as "–" rather than raising errors.

### Journal score (0–100)

Each component is scaled to 0–100, then combined as a weighted sum:

| Component | Weight | Score |
|---|---|---|
| Win rate | 20% | min(win rate ÷ 60%, 1) × 100 |
| Profit factor | 25% | min(profit factor ÷ 2.5, 1) × 100 (∞ → 100) |
| Avg win/loss | 20% | min(avg win/loss ratio ÷ 2.0, 1) × 100 (∞ → 100) |
| Consistency | 15% | (1 − best day's profit ÷ total profit of all green days) × 100 |
| Max drawdown | 20% | (1 − min(max drawdown ÷ gross profit, 1)) × 100; 0 if there is no gross profit |

```
score = 0.20·WinRate + 0.25·ProfitFactor + 0.20·AvgWinLoss + 0.15·Consistency + 0.20·Drawdown
```

*Consistency* rewards profits that are spread across many days. It is 0 when one day
produced all the profit and approaches 100 when many days contribute similar amounts.
*Drawdown* compares the worst drawdown to the profit you generated, so a drawdown that
gave back half of your gross profit scores 50. The targets (60% win rate, 2.5 profit
factor, 2.0 win/loss) are constants in `core/metrics.py` if you want to tune them.

## Project structure

```
app/                 Streamlit UI, one file per page
  main.py            entry point: navigation and sidebar filters
  dashboard.py  calendar_view.py  reports.py  trade_log.py  trade_detail.py
  journal.py  playbooks.py  import_trades.py  settings.py
  common.py          shared helpers (DB session, filters, formatting)
  charts.py          Plotly figure builders
core/
  models.py          SQLAlchemy 2.0 ORM models
  db.py              engine, sessions, init_db
  instruments.py     markets, contract multipliers, symbol resolution (ESZ6 → ES)
  importer.py        CSV parsing, column mapping, duplicate detection, execution grouping
  metrics.py         all statistics as pure functions on DataFrames
  repository.py      loads trades from the DB into DataFrames
  trades.py          tags, screenshots, playbook links, deletion
  journal.py         journal entries and playbook definitions
  seed.py            `trade-journal-seed` command
sample_data/         sample CSV (~200 USD trades across all five markets) and its generator
tests/               pytest suite (metrics, import grouping, DB operations, page smoke tests)
docs/screenshots/    images used in this README
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `TRADE_JOURNAL_DB_URL` | `sqlite:///data/journal.db` | Any SQLAlchemy URL |
| `TRADE_JOURNAL_DATA_DIR` | `data/` | Where the SQLite file and uploaded screenshots are stored |

The models only use portable column types. To move to Postgres, add a driver
(`uv add "psycopg[binary]"`) and set
`TRADE_JOURNAL_DB_URL=postgresql+psycopg://user:pass@host/trade_journal`. Tables are
created on first run.

## Development

```bash
uv run pytest                 # unit tests + Streamlit AppTest smoke tests of every page
uv run ruff check .           # lint
uv run ruff format .          # format
```

GitHub Actions (`.github/workflows/ci.yml`) runs ruff lint, the format check and
pytest on every push and pull request.

## Limitations

* All accounts are USD. Forex crosses without USD (EURGBP, EURJPY) are converted only
  when the export includes the broker's closed P&L (as order-history exports do).
  Otherwise they stay in their quote currency, because converting them needs a second
  exchange rate.
* Options are not modelled. An option symbol is treated as a stock with multiplier 1.
* Without a position ID column, grouping is per account and symbol, so two overlapping
  positions in the same symbol in one account are treated as one trade.
* Order-history exports only show a stop order's final price, so a stop you moved
  before it triggered is recorded at its last level (or left empty if it ended past
  the entry). Edit it on the trade page if you need the original risk.
