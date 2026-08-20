"""Permission and idempotency tests against the real HTTP app.

A broken permission check fails silently — the request just succeeds when it
shouldn't — so these assert the investor password genuinely cannot move the
fund, not merely that the UI hides the buttons.

Supabase and the price feed are replaced with in-memory fakes so the suite
stays fast and offline; the auth, routing and idempotency code under test is
the real thing.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402

from tests.fakes import FakeHub, FakeSupabase  # noqa: E402

SYM = "XAUUSD=X"


@pytest.fixture()
def client(monkeypatch):
    import main
    from engine import TradingEngine

    hub = FakeHub({SYM: 4000.0})
    sb = FakeSupabase()
    monkeypatch.setattr(main, "hub", hub)
    monkeypatch.setattr(main, "sb", sb)
    monkeypatch.setattr(main, "engine", TradingEngine(hub, sb))
    monkeypatch.setattr(main, "_idempotency_cache", {})

    # Both roles share one account number; the password decides the role.
    monkeypatch.setattr(main, "_get_credentials", lambda: {
        "account_number": "891962253",
        "admin_password_hash": _hash("admin-pw"),
        "investor_password_hash": _hash("investor-pw"),
    })

    c = TestClient(main.app)
    c.fake_sb = sb
    return c


def _hash(pw):
    import bcrypt
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def login(client, password):
    r = client.post("/api/login", json={"account_number": "891962253", "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- login ----------

def test_each_password_maps_to_its_own_role():
    pass  # covered by the two cases below, kept separate for clearer failures


def test_admin_password_grants_admin(client):
    r = client.post("/api/login", json={"account_number": "891962253", "password": "admin-pw"})
    assert r.json()["role"] == "admin"


def test_investor_password_grants_investor(client):
    r = client.post("/api/login", json={"account_number": "891962253", "password": "investor-pw"})
    assert r.json()["role"] == "investor"


def test_wrong_password_is_rejected(client):
    r = client.post("/api/login", json={"account_number": "891962253", "password": "nope"})
    assert r.status_code == 401


def test_wrong_account_number_is_rejected(client):
    r = client.post("/api/login", json={"account_number": "000000000", "password": "admin-pw"})
    assert r.status_code == 401


# ---------- the investor must not be able to move the fund ----------

TRADING_ENDPOINTS = [
    ("/api/order/market", {"symbol": SYM, "side": "buy", "qty": 1.0}),
    ("/api/order/pending", {"symbol": SYM, "order_type": "buy_limit", "qty": 1.0, "trigger_price": 1.0}),
    ("/api/position/close", {"ticket": 1}),
    ("/api/position/modify", {"ticket": 1, "sl": 1.0, "tp": 2.0}),
    ("/api/pending/cancel", {"ticket": 1}),
]


@pytest.mark.parametrize("path,body", TRADING_ENDPOINTS)
def test_investor_cannot_reach_trading_endpoints(client, path, body):
    token = login(client, "investor-pw")
    r = client.post(path, json=body, headers=auth(token))
    assert r.status_code == 403, f"{path} accepted an investor request"


@pytest.mark.parametrize("path,body", TRADING_ENDPOINTS)
def test_trading_endpoints_reject_anonymous_callers(client, path, body):
    assert client.post(path, json=body).status_code == 401


def test_investor_blocked_at_the_api_not_just_in_the_ui(client):
    """The read-only user bypassing the UI must still change nothing."""
    token = login(client, "investor-pw")
    before = client.fake_sb.balance
    client.post("/api/order/market", json={"symbol": SYM, "side": "buy", "qty": 1.0},
                headers=auth(token))
    assert client.fake_sb.positions == []
    assert client.fake_sb.balance == before


def test_investor_can_still_read_fund_state(client):
    token = login(client, "investor-pw")
    for path in ("/api/account", "/api/positions", "/api/history", "/api/journal"):
        assert client.get(path, headers=auth(token)).status_code == 200, path


def test_admin_can_trade(client):
    token = login(client, "admin-pw")
    r = client.post("/api/order/market", json={"symbol": SYM, "side": "buy", "qty": 1.0},
                    headers=auth(token))
    assert r.json()["ok"] is True
    assert len(client.fake_sb.positions) == 1


def test_a_tampered_token_is_rejected(client):
    token = login(client, "investor-pw")
    forged = token[:-6] + "AAAAAA"  # keep the shape, break the signature
    r = client.get("/api/account", headers=auth(forged))
    assert r.status_code == 401


# ---------- idempotency ----------

def test_replayed_request_id_does_not_open_a_second_position(client):
    token = login(client, "admin-pw")
    body = {"symbol": SYM, "side": "buy", "qty": 1.0, "request_id": "same-id"}

    first = client.post("/api/order/market", json=body, headers=auth(token)).json()
    second = client.post("/api/order/market", json=body, headers=auth(token)).json()

    assert first == second, "a duplicate should replay the original result"
    assert len(client.fake_sb.positions) == 1, "double submit opened two positions"


def test_distinct_request_ids_still_place_separate_orders(client):
    token = login(client, "admin-pw")
    for rid in ("a", "b"):
        client.post("/api/order/market",
                    json={"symbol": SYM, "side": "buy", "qty": 1.0, "request_id": rid},
                    headers=auth(token))
    assert len(client.fake_sb.positions) == 2, "deduplication swallowed a real order"


def test_replayed_close_does_not_settle_twice(client):
    token = login(client, "admin-pw")
    opened = client.post("/api/order/market",
                         json={"symbol": SYM, "side": "buy", "qty": 1.0, "request_id": "o1"},
                         headers=auth(token)).json()
    body = {"ticket": opened["ticket"], "request_id": "c1"}

    client.post("/api/position/close", json=body, headers=auth(token))
    balance_after_close = client.fake_sb.balance
    client.post("/api/position/close", json=body, headers=auth(token))

    assert client.fake_sb.balance == balance_after_close, "replayed close settled twice"
    assert len(client.fake_sb.history) == 1
