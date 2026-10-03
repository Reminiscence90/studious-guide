import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from core.models import JournalEntry, Playbook, Trade
from core.seed import seed


def test_seed_loads_sample_data(engine: Engine) -> None:
    count = seed(engine)
    assert count > 190
    with Session(engine) as s:
        assert s.scalar(select(func.count(Trade.id))) == count
        assert s.scalar(select(func.count(Playbook.id))) == 3
        assert s.scalar(select(func.count(JournalEntry.id))) > 20
        assert s.scalar(select(func.count(Trade.id)).where(Trade.stop_loss.is_not(None))) > 100


def test_seed_refuses_to_double_load(engine: Engine) -> None:
    seed(engine)
    with pytest.raises(SystemExit):
        seed(engine)
    assert seed(engine, reset=True) > 190
