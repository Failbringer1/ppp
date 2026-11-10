import os
import tempfile

import pytest

# must be set before tgads reads its config
os.environ["TGADS_DATA_DIR"] = tempfile.mkdtemp(prefix="tgads-test-")
os.environ["TGADS_RUN_WORKER"] = "0"
os.environ["TGADS_AI_MODE"] = "manual"


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    """Every test gets its own data dir and database."""
    from tgads import config, db

    monkeypatch.setenv("TGADS_DATA_DIR", tempfile.mkdtemp(prefix="tgads-test-"))
    config.get_config.cache_clear()
    db._engine, db._sessionmaker = None, None
    yield
    config.get_config.cache_clear()
    db._engine, db._sessionmaker = None, None


@pytest.fixture
def no_night(monkeypatch):
    """Flow tests publish their posts "now". Between 22:00 and 08:00 Tashkent time such posts are night posts
    (statistics only, no verdict), so without this the tests would depend on the hour they are run at."""
    from tgads.analysis import views

    monkeypatch.setattr(views, "NIGHT_HOURS", frozenset())
