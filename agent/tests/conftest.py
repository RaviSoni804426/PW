from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ongoingrec.config import Config
from ongoingrec.index import Database
from ongoingrec.timeutil import parse_timestamp

CORPUS_START = "2026-08-12T11:00:00Z"
CORPUS_SECONDS = 90 * 60


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated install root, so tests never touch a real installation."""
    root = tmp_path / "ongoingrec"
    monkeypatch.setenv("ONGOINGREC_HOME", str(root))
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def config(home: Path) -> Config:
    cfg = Config(
        email_id="abc@example.com",
        employee_id="EMP001",
        home=home,
        backend_base_url="http://127.0.0.1:9000",
    )
    cfg.ensure_dirs()
    cfg.save()
    return cfg


@pytest.fixture
def db(config: Config) -> Database:
    database = Database(config.db_path)
    yield database
    database.close()


@pytest.fixture(scope="session")
def _corpus(tmp_path_factory) -> Path:
    """90 minutes of watermarked audio, recorded once for the whole session.

    Encoding an hour and a half of MP3 is the slowest thing these tests do, and
    most of them only read it. Recording it once and copying the result per
    test keeps every test fully isolated without paying that cost repeatedly.
    """
    from .helpers import record_synthetic

    root = tmp_path_factory.mktemp("corpus")
    config = Config(email_id="abc@example.com", employee_id="EMP001", home=root)
    config.ensure_dirs()
    database = Database(config.db_path)
    record_synthetic(
        config, database, start=parse_timestamp(CORPUS_START), seconds=CORPUS_SECONDS
    )
    database.close()
    return root


@pytest.fixture
def recorded(config: Config, _corpus: Path) -> Database:
    """The shared corpus, copied into this test's own install root.

    Tests are free to delete segment files or mutate rows; the copy means the
    next test still gets a pristine recording.
    """
    shutil.copytree(_corpus / "recordings", config.recordings_dir, dirs_exist_ok=True)
    shutil.copy(_corpus / "data" / "index.db", config.db_path)

    database = Database(config.db_path)
    database.conn.execute(
        "UPDATE segments SET file_path = REPLACE(file_path, ?, ?)",
        (str(_corpus), str(config.home)),
    )
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _fixed_timezone(monkeypatch: pytest.MonkeyPatch):
    """Pin the timezone so naive-local timestamp handling is deterministic.

    IST is the deployment target and, having no DST, keeps the expected values
    in these tests stable wherever CI happens to run.
    """
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    if hasattr(os, "tzset"):
        os.tzset()
    yield
    if hasattr(os, "tzset"):
        os.tzset()
