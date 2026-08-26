"""Shared test fixtures.

Every test gets a throwaway database so nothing leaks between scenarios, and the
session settle delay is switched off by default (it is five real seconds in
production; the tests that care about it set their own value).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The unofficial clients talk to live servers and are not needed to exercise our
# own logic, so they are stubbed before any project module is imported.
import stubs  # noqa: E402

stubs.install()

import busy  # noqa: E402
import central_db  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Point both databases at a temp dir and initialise them."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "customer.db"))
    monkeypatch.setattr(central_db, "DB_PATH", str(tmp_path / "central.db"))
    monkeypatch.setattr(config, "SESSION_SETTLE_SEC", 0.0)
    busy.clear_all()
    db.init()
    central_db.init()
    yield
    busy.clear_all()


@pytest.fixture
def alice():
    """A customer with a fresh trial."""
    return db.ensure_customer(1001, "Alice", "alice")["telegram_id"]


@pytest.fixture
def bob():
    """A second customer — used to prove nothing crosses between them."""
    return db.ensure_customer(2002, "Bob", "bob")["telegram_id"]
