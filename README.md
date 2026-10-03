# Trade Journal

A personal trading journal and analytics app inspired by Tradezella, built with
Streamlit, SQLAlchemy and pandas. Import your broker's CSV, tag trades, keep a daily
journal, define strategy playbooks and see where your edge is.

![Dashboard](docs/screenshots/dashboard.png)

## Features

| Area | What you get |
|---|---|
| **Import** | CSV import with a column-mapping screen and reusable *broker presets*; manual trade entry; partial fills grouped into trades; duplicate rows skipped; multiple accounts, each with its own currency (default MYR) |
| **Dashboard** | Net P&L, win rate, profit factor, avg win, avg loss, avg win/loss ratio, expectancy, total trades, max drawdown, cumulative P&L curve with drawdown shading, daily P&L bars and a 0–100 *journal score* radar |
| **Calendar** | Monthly calendar with per-day net P&L, trade count and journal mood, colored green/red, with weekly totals. Click a day to see its trades and journal entry |
| **Trade log** | Sortable, filterable table (symbol, side, status, result, tags, playbook) with CSV export. Select a row to open the trade |
| **Trade detail** | Entry/exit, P&L, fees, R-multiple (once a stop loss is set), holding time, markdown notes, screenshot uploads (stored locally), tags, playbook checklist, executions |
| **Tags** | Setups, Mistakes and Emotions, with custom tags; several tags per trade |
| **Daily journal** | One entry per trading day: pre-market plan, post-market review, mood (1–5) and notes |
| **Playbooks** | Name, description, entry/exit rules and a checklist. Link trades, tick the criteria that were met, and see per-playbook stats and checklist adherence |
| **Reports** | P&L, win rate and trade count by symbol, day of week, entry hour, holding time, long vs short, each tag and month |

The **account** and **date range** filters in the sidebar apply to every page.

## Quick start

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 if you don't have it.

```bash
git clone <this repo> trade-journal && cd trade-journal
uv sync                         # create .venv and install dependencies
uv run trade-journal-seed       # load ~200 sample trades, tags, playbooks and journal entries
uv run streamlit run app/main.py
```

Open http://localhost:8501. To start again from the sample data, run
`uv run trade-journal-seed --reset` (this wipes the database). To start with an empty
journal, skip the seed step; the app creates a default `Main` account in MYR.

## Screenshots

| | |
|---|---|
| ![Calendar](docs/screenshots/calendar.png) **Calendar** | ![Day details](docs/screenshots/calendar_day.png) **Clicking a day** |
| ![Trade log](docs/screenshots/trade_log.png) **Trade log** | ![Trade detail](docs/screenshots/trade_detail.png) **Trade detail** |
| ![Reports](docs/screenshots/reports_tags.png) **Reports: tags** | ![Playbooks](docs/screenshots/playbooks.png) **Playbooks** |
| ![Import](docs/screenshots/import_mapping.png) **CSV column mapping** | ![Journal](docs/screenshots/journal.png) **Daily journal** |

## CSV format

Any CSV with a header row works. On the **Import** page, pick which column holds each
field (common header names are detected for you), then optionally save the mapping as
a broker preset so it's prefilled next time.

| Field | Required | Notes |
|---|---|---|
| Symbol | yes | Upper-cased on import |
| Side | yes | `buy`/`sell`, `long`/`short`, `B`/`S`, `BOT`/`SLD`, `BTO`/`STC`, `SS`… (case-insensitive) |
| Quantity | yes | Thousands separators allowed; a negative quantity is treated as its absolute value |
| Entry price | yes | The fill price for one-row-per-execution files |
| Entry time | yes | Auto-detected, or give a `strftime` format such as `%d/%m/%Y %H:%M` |
| Exit price | no | Map together with exit time for round-trip rows |
| Exit time | no | |
| Fees | no | Commission plus other charges. On round-trip rows, half goes to each leg |

Two layouts are supported:

1. **One row per execution (fill).** Map symbol, side, quantity, entry price/time and
   fees; leave exit price/time unmapped.

   ```csv
   Symbol,Action,Qty,Price,Time,Commission
   MAYBANK,BUY,1000,10.20,2026-06-01 09:31:02,12.50
   MAYBANK,BUY,1000,10.24,2026-06-01 09:33:40,12.50
   MAYBANK,SELL,2000,10.40,2026-06-01 10:05:11,25.00
   ```

2. **One row per round trip.** Also map exit price and exit time. The side is the
   direction of the position (`Long`/`Buy` opens with a buy). A row with an empty
   exit is an open position.

   ```csv
   Ticker,Direction,Qty,Entry Price,Open Time,Exit Price,Close Time,Commission
   GAMUDA,Short,2000,5.110,2026-05-04 09:10:33,5.000,2026-05-04 09:17:33,44.24
   ```

`sample_data/sample_trades.csv` uses the second layout. It has 257 rows that group into
207 trades, because some positions were scaled out of across several rows. It was made
by `sample_data/generate_sample.py`.

### How trades are built

* **Grouping.** Each row becomes one execution, or two for a round-trip row. The
  executions of each symbol are walked in time order while tracking the net position.
  A trade starts when the position leaves zero and ends when it returns to zero, so all
  partial fills in between belong to one trade. A fill that flips the position (selling
  150 while long 100) is split: 100 closes the long, 50 opens a new short.
* **Prices and P&L.** Average entry and exit are volume-weighted. Gross P&L is
  (avg exit − avg entry) × closed quantity, sign-adjusted for shorts. Net P&L is gross
  P&L minus all fees.
* **Open positions.** A trade that hasn't returned to flat is stored as *open* and
  **left out of every statistic**. If a later import closes it, the executions are
  added to the same trade, so its notes and tags are kept.
* **Duplicates.** Every execution gets a fingerprint (account, symbol, side, quantity,
  price, time, fees). Fingerprints that are already in the account are skipped, so
  re-importing an overlapping export is safe. Identical rows within one file are told
  apart by how many times they occur, so two genuine identical fills both import.

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
| R-multiple | net P&L ÷ (\|avg entry − stop loss\| × quantity). Only shown once a stop loss is set |
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
  importer.py        CSV parsing, column mapping, duplicate detection, execution grouping
  metrics.py         all statistics as pure functions on DataFrames
  repository.py      loads trades from the DB into DataFrames
  trades.py          tags, screenshots, playbook links, deletion
  journal.py         journal entries and playbook definitions
  seed.py            `trade-journal-seed` command
sample_data/         sample CSV (~200 trades) and its generator
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

* Accounts with different currencies are summed **without FX conversion**. The app
  warns you when the filter mixes currencies.
* There is no contract multiplier, so futures and options P&L assume a multiplier of 1.
* Grouping is per account and symbol, so two overlapping positions in the same symbol
  in one account are treated as one trade.
