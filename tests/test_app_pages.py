"""Smoke tests: every Streamlit page renders against the seeded sample database."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import streamlit as st
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


@pytest.fixture(autouse=True)
def _fresh_app_engine() -> None:
    """The app caches its engine per process; drop it so each test uses its own database."""
    st.cache_resource.clear()


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


def test_add_account_saves_it(seeded_db: None) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("settings.py").run()
    at.text_input(key="new_account_name").input("Topstep")
    next(b for b in at.button if b.label == "Add account").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Topstep" in at.multiselect(key="filter_accounts").options


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_on_an_empty_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: str
) -> None:
    monkeypatch.setenv("TRADE_JOURNAL_DB_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page(page).run()
    assert not at.exception, [e.value for e in at.exception]


def test_trade_detail_edits_are_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from core.models import Trade

    url = f"sqlite:///{tmp_path / 'edit.db'}"
    monkeypatch.setenv("TRADE_JOURNAL_DB_URL", url)
    seed(get_engine(url))
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("trade_detail.py").run()
    trade_id = at.selectbox(key="trade_picker").value

    at.text_area(key=f"notes_{trade_id}").input("Waited for the retest.")
    next(b for b in at.button if b.label == "Save notes").click().run()
    at.multiselect(key=f"tags_mistake_{trade_id}").select("FOMO entry")
    next(b for b in at.button if b.label == "Save tags").click().run()
    assert not at.exception, [e.value for e in at.exception]
    with Session(get_engine(url)) as s:
        trade = s.get(Trade, trade_id)
        assert trade is not None
        assert trade.notes == "Waited for the retest."
        assert "FOMO entry" in {t.name for t in trade.tags}

    next(b for b in at.button if b.label == "Confirm delete").click().run()
    assert not at.exception, [e.value for e in at.exception]
    with Session(get_engine(url)) as s:
        assert s.get(Trade, trade_id) is None
        assert s.scalars(select(Trade)).first() is not None
