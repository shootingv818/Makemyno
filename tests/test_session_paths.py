"""
Session storage is namespaced per customer.

THE FAILURE THIS PREVENTS
-------------------------
In the base project a session file was named only after the phone number, in one
flat folder. Two customers who own the same number (SIMs get resold and shared)
therefore pointed at the SAME file. Each login kicked the other out, forever,
and neither customer could be told why — it just looked like the service was
broken.
"""
import os

import pytest

import account_conn


class _FakeRB:
    """Stand-in for rubika_client: the real one needs the rubpy package."""

    def __init__(self, root):
        self.root = root
        self.opened = []

    @staticmethod
    def normalize_phone(phone):
        p = "".join(ch for ch in str(phone) if ch.isdigit())
        return "98" + p[1:] if p.startswith("0") else p

    def session_dir(self, customer_id):
        cid = int(customer_id or 0)
        if not cid:
            raise ValueError("customer id required")
        path = os.path.join(self.root, f"c{cid}")
        os.makedirs(path, exist_ok=True)
        return path

    def session_path(self, phone, customer_id):
        safe = "".join(ch for ch in str(phone) if ch.isdigit())
        return os.path.join(self.session_dir(customer_id), f"acc_{safe}")

    def open_client(self, phone, customer_id):
        path = self.session_path(phone, customer_id)
        self.opened.append(path)
        return object()


@pytest.fixture
def fake_rb(tmp_path, monkeypatch):
    fake = _FakeRB(str(tmp_path / "sessions"))
    monkeypatch.setattr(account_conn, "rb", fake)
    account_conn._conns.clear()
    yield fake
    account_conn._conns.clear()


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def test_same_number_two_customers_two_files(fake_rb):
    a = fake_rb.session_path("09121110000", 1001)
    b = fake_rb.session_path("09121110000", 2002)
    assert a != b
    assert os.path.dirname(a) != os.path.dirname(b)
    assert os.path.basename(a) == os.path.basename(b)   # same account name


def test_session_path_ignores_number_formatting(fake_rb):
    assert fake_rb.session_path("+98 912 111 0000", 1) == \
           fake_rb.session_path("989121110000", 1)


def test_session_dir_refuses_a_missing_customer(fake_rb):
    """No customer id must mean a loud error, never a shared folder."""
    for bad in (None, 0, ""):
        with pytest.raises(ValueError):
            fake_rb.session_dir(bad)


def test_real_rubika_client_enforces_the_same_rule():
    """Check the shipped module too, not just the test double."""
    import importlib.util
    spec = importlib.util.find_spec("rubika_client")
    assert spec is not None
    src_path = spec.origin
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    # both helpers must require the customer id
    assert "def session_dir(customer_id)" in src
    assert "def session_path(phone: str, customer_id)" in src
    assert "def open_client(phone: str, customer_id)" in src
    # and no caller may fall back to a flat, phone-only path
    assert "session_path(phone)" not in src


# --------------------------------------------------------------------------- #
# The warm-connection cache is keyed by (customer, phone)
# --------------------------------------------------------------------------- #
def test_connection_cache_key_includes_the_customer(fake_rb):
    k1 = account_conn._key(1001, "09121110000")
    k2 = account_conn._key(2002, "09121110000")
    assert k1 != k2
    assert k1.startswith("1001:")


def test_two_customers_get_separate_conn_objects(fake_rb):
    """Sharing one warm client between customers would hand B the socket that A
    is using — an instant AUTH_FROM_ANOTHER for whoever connected first."""
    c1 = account_conn._get_conn(1001, "09121110000")
    c2 = account_conn._get_conn(2002, "09121110000")
    assert c1 is not c2
    assert c1.lock is not c2.lock
    assert c1.customer_id == 1001 and c2.customer_id == 2002


def test_same_customer_and_phone_reuses_one_conn(fake_rb):
    first = account_conn._get_conn(1001, "09121110000")
    again = account_conn._get_conn(1001, "+989121110000")
    assert first is again          # normalised to the same session


def test_conn_requires_a_customer_id(fake_rb):
    for bad in (None, 0, "", "abc"):
        with pytest.raises(ValueError):
            account_conn._get_conn(bad, "09121110000")


def test_invalid_flag_is_per_customer(fake_rb):
    account_conn._get_conn(1001, "0912").invalid = True
    assert account_conn.is_invalid(1001, "0912") is True
    assert account_conn.is_invalid(2002, "0912") is False


def test_reset_invalid_is_scoped(fake_rb):
    account_conn._get_conn(1001, "0912").invalid = True
    account_conn.reset_invalid(2002, "0912")           # wrong customer: no effect
    assert account_conn.is_invalid(1001, "0912") is True
    account_conn.reset_invalid(1001, "0912")
    assert account_conn.is_invalid(1001, "0912") is False


def test_is_invalid_on_an_unknown_session_is_false(fake_rb):
    assert account_conn.is_invalid(1001, "09120000000") is False


def test_open_count_reports_live_sockets(fake_rb):
    assert account_conn.open_count() == 0
    account_conn._get_conn(1001, "0912").client = object()
    account_conn._get_conn(2002, "0912").client = object()
    assert account_conn.open_count() == 2


def test_drop_connection_only_touches_the_named_session(fake_rb):
    a = account_conn._get_conn(1001, "0912")
    b = account_conn._get_conn(2002, "0912")
    a.client = object()
    b.client = object()
    account_conn.drop_connection(1001, "0912")
    assert a.client is None
    assert b.client is not None


def test_drop_connection_on_unknown_session_is_a_no_op(fake_rb):
    account_conn.drop_connection(1001, "09999999999")   # must not raise


# --------------------------------------------------------------------------- #
# Auth-error classification stays narrow
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message", [
    "INVALID_AUTH", "invalidauth", "NOT_REGISTERED", "AUTH_FROM_ANOTHER device",
])
def test_real_auth_failures_are_recognised(message):
    assert account_conn.is_auth_error(Exception(message)) is True


@pytest.mark.parametrize("message", [
    "timeout", "connection reset by peer", "GROUP_IS_MUTED",
    "ACCESS_DENIED for this chat", "rate limited", "unauthorized-ish text",
])
def test_transient_and_group_level_errors_are_not_auth_failures(message):
    """Misclassifying these is how healthy accounts get quarantined."""
    assert account_conn.is_auth_error(Exception(message)) is False
