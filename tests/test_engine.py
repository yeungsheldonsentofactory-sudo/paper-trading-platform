"""Engine tests: the money math and the trigger conditions.

These are the parts that fail silently — a wrong PnL still looks like a
number, and an SL that doesn't fire just leaves a position open. Prices are
fixed per test so every assertion is an exact expected value.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import STOP_OUT_LEVEL, TradingEngine  # noqa: E402
from tests.fakes import FakeHub, FakeSupabase  # noqa: E402

SYM = "XAUUSD=X"


def make(balance=100_000.0, price=4000.0, leverage=100.0):
    hub = FakeHub({SYM: price})
    sb = FakeSupabase(balance=balance, leverage=leverage)
    return TradingEngine(hub, sb), hub, sb


def bid_ask(price, bps=5):
    half = price * (bps / 10_000)
    return price - half, price + half


# ---------- fills use the correct side of the spread ----------

def test_buy_fills_at_ask_not_mid():
    engine, hub, sb = make(price=4000.0)
    _, ask = bid_ask(4000.0)
    pos = engine.place_market_order(SYM, "buy", 1.0)
    assert pos["entry_price"] == pytest.approx(ask)
    assert pos["entry_price"] > 4000.0, "a buy filling at mid would flatter results"


def test_sell_fills_at_bid_not_mid():
    engine, hub, sb = make(price=4000.0)
    bid, _ = bid_ask(4000.0)
    pos = engine.place_market_order(SYM, "sell", 1.0)
    assert pos["entry_price"] == pytest.approx(bid)
    assert pos["entry_price"] < 4000.0


def test_open_then_immediately_close_loses_the_spread():
    """Round-tripping with no price move must cost the spread, never profit."""
    engine, hub, sb = make(price=4000.0)
    pos = engine.place_market_order(SYM, "buy", 1.0)
    result = engine.close_position(pos["ticket"])
    bid, ask = bid_ask(4000.0)
    assert result["pnl"] == pytest.approx(bid - ask)
    assert result["pnl"] < 0


# ---------- PnL direction and magnitude ----------

def test_long_pnl_uses_bid_and_scales_with_qty():
    engine, hub, sb = make(price=4000.0)
    pos = engine.place_market_order(SYM, "buy", 2.0)
    hub.set(SYM, 4100.0)
    new_bid, _ = bid_ask(4100.0)
    expected = (new_bid - pos["entry_price"]) * 2.0
    assert engine._position_floating(pos) == pytest.approx(expected)
    assert expected > 0, "price up should profit a long"


def test_short_profits_when_price_falls():
    engine, hub, sb = make(price=4000.0)
    pos = engine.place_market_order(SYM, "sell", 1.0)
    hub.set(SYM, 3900.0)
    _, new_ask = bid_ask(3900.0)
    expected = (new_ask - pos["entry_price"]) * 1.0 * -1
    assert engine._position_floating(pos) == pytest.approx(expected)
    assert expected > 0


def test_realised_pnl_is_credited_to_balance_exactly_once():
    engine, hub, sb = make(balance=100_000.0, price=4000.0)
    pos = engine.place_market_order(SYM, "buy", 1.0)
    hub.set(SYM, 4200.0)
    result = engine.close_position(pos["ticket"])
    assert sb.balance == pytest.approx(100_000.0 + result["pnl"])
    assert len(sb.history) == 1
    assert sb.history[0]["pnl"] == pytest.approx(result["pnl"])


def test_closing_a_short_at_a_lower_price_realises_a_profit():
    """Unrealised PnL flips sign for shorts, and so must the realised side."""
    engine, hub, sb = make(balance=100_000.0, price=4000.0)
    pos = engine.place_market_order(SYM, "sell", 1.0)
    hub.set(SYM, 3800.0)
    _, close_ask = bid_ask(3800.0)

    result = engine.close_position(pos["ticket"])

    assert result["pnl"] == pytest.approx((close_ask - pos["entry_price"]) * 1.0 * -1)
    assert result["pnl"] > 0, "a short closed lower must realise a profit"
    assert sb.balance == pytest.approx(100_000.0 + result["pnl"])


def test_closing_a_short_at_a_higher_price_realises_a_loss():
    engine, hub, sb = make(balance=100_000.0, price=4000.0)
    pos = engine.place_market_order(SYM, "sell", 1.0)
    hub.set(SYM, 4200.0)

    result = engine.close_position(pos["ticket"])

    assert result["pnl"] < 0, "a short closed higher must realise a loss"
    assert sb.balance == pytest.approx(100_000.0 + result["pnl"])
    assert sb.balance < 100_000.0


def test_closing_a_short_via_stop_loss_realises_a_loss():
    engine, hub, sb = make(balance=100_000.0, price=4000.0)
    engine.place_market_order(SYM, "sell", 1.0, sl=4100.0)
    hub.set(SYM, 4150.0)

    engine.process_tick()

    assert sb.history[0]["reason"] == "sl"
    assert sb.history[0]["pnl"] < 0, "stopped-out short recorded a profit"
    assert sb.balance < 100_000.0


def test_closed_trade_keeps_its_inputs_not_just_the_pnl():
    """Stored inputs are what make a bad PnL fixable after the fact."""
    engine, hub, sb = make(price=4000.0)
    pos = engine.place_market_order(SYM, "buy", 1.0)
    hub.set(SYM, 4050.0)
    engine.close_position(pos["ticket"])
    row = sb.history[0]
    for field in ("entry_price", "close_price", "qty", "side", "symbol"):
        assert row.get(field) is not None, f"{field} missing from trade record"


# ---------- partial close ----------

def test_partial_close_settles_only_the_closed_size():
    engine, hub, sb = make(price=4000.0)
    pos = engine.place_market_order(SYM, "buy", 3.0)
    hub.set(SYM, 4100.0)
    before = sb.balance
    result = engine.close_position(pos["ticket"], qty=1.0)

    new_bid, _ = bid_ask(4100.0)
    assert result["pnl"] == pytest.approx((new_bid - pos["entry_price"]) * 1.0)
    assert sb.balance == pytest.approx(before + result["pnl"])
    assert sb.positions[0]["qty"] == pytest.approx(2.0), "remainder should stay open"


# ---------- SL / TP triggers ----------

def test_long_stop_loss_fires_when_bid_crosses():
    engine, hub, sb = make(price=4000.0)
    pos = engine.place_market_order(SYM, "buy", 1.0, sl=3900.0)
    hub.set(SYM, 3800.0)
    engine.process_tick()
    assert sb.positions == [], "stop loss did not close the position"
    assert sb.history[0]["reason"] == "sl"


def test_long_take_profit_fires_when_bid_crosses():
    engine, hub, sb = make(price=4000.0)
    engine.place_market_order(SYM, "buy", 1.0, tp=4100.0)
    hub.set(SYM, 4200.0)
    engine.process_tick()
    assert sb.positions == []
    assert sb.history[0]["reason"] == "tp"


def test_short_stop_loss_fires_on_ask_not_bid():
    engine, hub, sb = make(price=4000.0)
    engine.place_market_order(SYM, "sell", 1.0, sl=4100.0)
    hub.set(SYM, 4200.0)
    engine.process_tick()
    assert sb.positions == []
    assert sb.history[0]["reason"] == "sl"


def test_sl_does_not_fire_while_price_is_short_of_the_trigger():
    engine, hub, sb = make(price=4000.0)
    engine.place_market_order(SYM, "buy", 1.0, sl=3900.0)
    hub.set(SYM, 3950.0)  # closer, but not through the stop
    engine.process_tick()
    assert len(sb.positions) == 1, "stop fired early"


def test_long_sl_fires_when_bid_sits_exactly_on_the_trigger():
    """Touching the stop must close: `<` instead of `<=` would leave it open."""
    engine, hub, sb = make(price=4000.0)
    trigger = 3900.0
    engine.place_market_order(SYM, "buy", 1.0, sl=trigger)
    hub.set_bid_ask(SYM, bid=trigger, ask=trigger + 2.0)
    assert hub.get_bid_ask(SYM)[0] == trigger  # exact, not approx
    engine.process_tick()
    assert sb.positions == [], "stop did not fire when bid touched the trigger"


def test_long_tp_fires_when_bid_sits_exactly_on_the_trigger():
    engine, hub, sb = make(price=4000.0)
    trigger = 4100.0
    engine.place_market_order(SYM, "buy", 1.0, tp=trigger)
    hub.set_bid_ask(SYM, bid=trigger, ask=trigger + 2.0)
    engine.process_tick()
    assert sb.positions == [], "take profit did not fire on an exact touch"


def test_short_sl_fires_when_ask_sits_exactly_on_the_trigger():
    engine, hub, sb = make(price=4000.0)
    trigger = 4100.0
    engine.place_market_order(SYM, "sell", 1.0, sl=trigger)
    hub.set_bid_ask(SYM, bid=trigger - 2.0, ask=trigger)
    engine.process_tick()
    assert sb.positions == [], "short stop did not fire on an exact touch"


def test_long_sl_holds_one_tick_short_of_the_trigger():
    """The other half of the boundary: `<=` must not become `>=`-ish either."""
    engine, hub, sb = make(price=4000.0)
    trigger = 3900.0
    engine.place_market_order(SYM, "buy", 1.0, sl=trigger)
    hub.set_bid_ask(SYM, bid=trigger + 0.01, ask=trigger + 2.0)
    engine.process_tick()
    assert len(sb.positions) == 1, "stop fired before the trigger was reached"


def test_position_without_sl_or_tp_is_left_alone():
    engine, hub, sb = make(price=4000.0)
    engine.place_market_order(SYM, "buy", 1.0)
    hub.set(SYM, 100.0)  # violent move, but nothing was configured to fire
    engine._check_sl_tp(sb.positions)
    assert len(sb.positions) == 1


# ---------- margin ----------

def test_order_is_rejected_when_margin_is_insufficient():
    engine, hub, sb = make(balance=1_000.0, price=4000.0, leverage=100.0)
    # 100 units at ~4000 needs ~4000 margin at 1:100, well over the balance.
    with pytest.raises(ValueError, match="insufficient margin"):
        engine.place_market_order(SYM, "buy", 100.0)
    assert sb.positions == [], "rejected order must not leave a position behind"


def test_margin_used_reflects_leverage():
    engine, hub, sb = make(balance=100_000.0, price=4000.0, leverage=100.0)
    engine.place_market_order(SYM, "buy", 1.0)
    summary = engine.fund_summary()
    assert summary["margin_used"] == pytest.approx(4000.0 / 100.0, rel=1e-3)


def test_equity_is_balance_plus_floating():
    engine, hub, sb = make(balance=100_000.0, price=4000.0)
    engine.place_market_order(SYM, "buy", 1.0)
    hub.set(SYM, 4100.0)
    summary = engine.fund_summary()
    assert summary["equity"] == pytest.approx(
        summary["balance"] + summary["floating_pnl"]
    )


# ---------- input validation ----------

@pytest.mark.parametrize("qty", [0, -1, -0.01])
def test_non_positive_quantity_is_rejected(qty):
    engine, hub, sb = make()
    with pytest.raises(ValueError):
        engine.place_market_order(SYM, "buy", qty)


def test_unknown_side_is_rejected():
    engine, hub, sb = make()
    with pytest.raises(ValueError):
        engine.place_market_order(SYM, "sideways", 1.0)


def test_order_on_a_symbol_with_no_price_is_rejected():
    engine, hub, sb = make()
    with pytest.raises(ValueError, match="no price"):
        engine.place_market_order("NOSUCH", "buy", 1.0)


# ---------- stop-out ----------

def test_stop_out_closes_positions_once_margin_level_collapses():
    engine, hub, sb = make(balance=5_000.0, price=4000.0, leverage=100.0)
    engine.place_market_order(SYM, "buy", 10.0)
    hub.set(SYM, 3520.0)  # loss deep enough to take margin level under 50%

    summary = engine.fund_summary()
    assert summary["margin_level"] < STOP_OUT_LEVEL, "test setup did not breach stop-out"

    engine.process_tick()
    assert sb.positions == []
    assert sb.history[0]["reason"] == "stop_out"
