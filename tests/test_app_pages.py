"""Smoke tests: every Streamlit page renders against the seeded sample database."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core.db import get_engine
from core.seed import seed

APP = str(Path(__file__).resolve().parent.parent / "app" / "main.py")
PAGES = [
    "dashboard.py",
    "calendar_view.py",
    "reports.py",
    "trade_log.py",
    "trade_detail.py",
    "journal.py",
    "playbooks.py",
    "import_trades.py",
    "settings.py",
]


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    db_path = tmp_path_factory.mktemp("app") / "journal.db"
    url = f"sqlite:///{db_path}"
    mp = pytest.MonkeyPatch()
    mp.setenv("TRADE_JOURNAL_DB_URL", url)
    seed(get_engine(url))
    yield
    mp.undo()


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_without_exceptions(seeded_db: None, page: str) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page(page).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title, f"{page} rendered no title"


def test_dashboard_shows_headline_stats(seeded_db: None) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    labels = {metric.label for metric in at.metric}
    assert {"Net P&L", "Win rate", "Profit factor", "Max drawdown", "Journal score"} <= labels


def test_date_filter_applies(seeded_db: None) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    total_all = next(mt.value for mt in at.metric if mt.label == "Total trades")
    at.selectbox(key="filter_preset").select("Last 30 days").run()
    total_30 = next(mt.value for mt in at.metric if mt.label == "Total trades")
    assert int(total_30) < int(total_all)


def test_markets_filter_applies(seeded_db: None) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    total_all = int(next(mt.value for mt in at.metric if mt.label == "Total trades"))
    at.multiselect(key="filter_assets").select("forex").run()
    total_fx = int(next(mt.value for mt in at.metric if mt.label == "Total trades"))
    assert 0 < total_fx < total_all
