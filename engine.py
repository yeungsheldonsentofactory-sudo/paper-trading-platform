import time

DEFAULT_LEVERAGE = 100.0
STOP_OUT_LEVEL = 50.0  # margin level % below which positions are force-closed
MARGIN_CALL_LEVEL = 100.0
JOURNAL_MAX = 300

REASON_LABEL = {"manual": "手動平倉", "sl": "觸發停損", "tp": "觸發停利", "stop_out": "強制平倉"}


class TradingEngine:
    """A single shared fund/portfolio, backed by Supabase Postgres.

    There are no per-user accounts any more — one admin trades the fund,
    everyone else just reads its state. All methods here are synchronous
    (supabase-py makes blocking HTTP calls); callers on the async side
    should run them via asyncio.to_thread.
    """

    def __init__(self, market_data_hub, supabase_client):
        self.hub = market_data_hub
        self.sb = supabase_client

    # ---------- low-level table helpers ----------

    def _log(self, message: str):
        self.sb.table("journal").insert({"time": _now_iso(), "message": message}).execute()

    def _fund_row(self) -> dict:
        res = self.sb.table("fund_account").select("*").eq("id", 1).single().execute()
        return res.data

    # ---------- pricing helpers ----------

    def _bid_ask(self, symbol: str) -> tuple[float, float]:
        ba = self.hub.get_bid_ask(symbol)
        if ba is None:
            raise ValueError(f"no price available for {symbol}")
        return ba

    # ---------- exposure / margin ----------

    def _positions_raw(self) -> list[dict]:
        return self.sb.table("positions").select("*").execute().data

    def _floating_pnl(self, positions: list[dict] | None = None) -> float:
        total = 0.0
        for pos in (positions if positions is not None else self._positions_raw()):
            ba = self.hub.get_bid_ask(pos["symbol"])
            if ba is None:
                continue
            bid, ask = ba
            exit_price = bid if pos["side"] == "buy" else ask
            sign = 1 if pos["side"] == "buy" else -1
            total += (exit_price - pos["entry_price"]) * pos["qty"] * sign
        return total

    def _margin_used(self, leverage: float, positions: list[dict] | None = None) -> float:
        total = 0.0
        for pos in (positions if positions is not None else self._positions_raw()):
            price = self.hub.get_price(pos["symbol"])
            if price is None:
                continue
            total += pos["qty"] * price / leverage
        return total

    def fund_summary(self, fund: dict | None = None, positions: list[dict] | None = None) -> dict:
        # Callers that already hold the fund row / positions pass them in, so a
        # single logical operation doesn't re-query the same rows several times.
        fund = fund if fund is not None else self._fund_row()
        positions = positions if positions is not None else self._positions_raw()
        floating = self._floating_pnl(positions)
        margin_used = self._margin_used(fund["leverage"], positions)
        equity = fund["balance"] + floating
        free_margin = equity - margin_used
        margin_level = (equity / margin_used * 100) if margin_used > 0 else None
        return {
            "balance": fund["balance"],
            "equity": equity,
            "floating_pnl": floating,
            "margin_used": margin_used,
            "free_margin": free_margin,
            "margin_level": margin_level,
            "leverage": fund["leverage"],
        }

    # ---------- orders ----------

    def place_market_order(self, symbol: str, side: str, qty: float,
                            sl: float | None = None, tp: float | None = None) -> dict:
        if qty <= 0:
            raise ValueError("qty must be positive")
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")

        bid, ask = self._bid_ask(symbol)
        entry_price = ask if side == "buy" else bid

        fund = self._fund_row()
        positions = self._positions_raw()
        required_margin = qty * entry_price / fund["leverage"]
        summary = self.fund_summary(fund, positions)
        if required_margin > summary["free_margin"]:
            raise ValueError("insufficient margin")

        res = self.sb.table("positions").insert({
            "symbol": symbol, "side": side, "qty": qty, "entry_price": entry_price,
            "sl": sl, "tp": tp,
        }).execute()
        pos = res.data[0]
        self._log(f"#{pos['ticket']} 市價{'買進' if side=='buy' else '賣出'} {qty} {symbol} @ {entry_price:.5f}")
        return pos

    def place_pending_order(self, symbol: str, order_type: str, qty: float,
                             trigger_price: float, sl: float | None = None,
                             tp: float | None = None) -> dict:
        if qty <= 0:
            raise ValueError("qty must be positive")
        if order_type not in ("buy_limit", "sell_limit", "buy_stop", "sell_stop"):
            raise ValueError("invalid order_type")

        res = self.sb.table("pending_orders").insert({
            "symbol": symbol, "order_type": order_type, "qty": qty,
            "trigger_price": trigger_price, "sl": sl, "tp": tp,
        }).execute()
        order = res.data[0]
        self._log(f"#{order['ticket']} 掛單 {order_type} {qty} {symbol} @ {trigger_price:.5f}")
        return order

    def cancel_pending(self, ticket: int):
        res = self.sb.table("pending_orders").select("*").eq("ticket", ticket).execute()
        if not res.data:
            raise ValueError("pending order not found")
        self.sb.table("pending_orders").delete().eq("ticket", ticket).execute()
        self._log(f"#{ticket} 掛單已取消")

    def modify_position(self, ticket: int, sl: float | None, tp: float | None) -> dict:
        res = self.sb.table("positions").select("*").eq("ticket", ticket).execute()
        if not res.data:
            raise ValueError("position not found")
        self.sb.table("positions").update({"sl": sl, "tp": tp}).eq("ticket", ticket).execute()
        self._log(f"#{ticket} 修改 SL={sl if sl is not None else '-'} TP={tp if tp is not None else '-'}")
        return res.data[0]

    def close_position(self, ticket: int, reason: str = "manual", qty: float | None = None) -> dict:
        return self._close(ticket, reason, qty)

    def _close(self, ticket: int, reason: str, qty: float | None = None) -> dict:
        res = self.sb.table("positions").select("*").eq("ticket", ticket).execute()
        if not res.data:
            raise ValueError("position not found")
        pos = res.data[0]

        close_qty = pos["qty"] if (qty is None or qty >= pos["qty"]) else qty
        if close_qty <= 0:
            raise ValueError("qty must be positive")
        is_partial = close_qty < pos["qty"]

        bid, ask = self._bid_ask(pos["symbol"])
        close_price = bid if pos["side"] == "buy" else ask
        sign = 1 if pos["side"] == "buy" else -1
        pnl = (close_price - pos["entry_price"]) * close_qty * sign

        fund = self._fund_row()
        self.sb.table("fund_account").update({"balance": fund["balance"] + pnl}).eq("id", 1).execute()

        self.sb.table("trade_history").insert({
            "ticket": pos["ticket"], "symbol": pos["symbol"], "side": pos["side"],
            "qty": close_qty, "entry_price": pos["entry_price"], "close_price": close_price,
            "open_time": pos["open_time"], "pnl": pnl, "reason": reason,
        }).execute()

        if is_partial:
            self.sb.table("positions").update({"qty": pos["qty"] - close_qty}).eq("ticket", ticket).execute()
        else:
            self.sb.table("positions").delete().eq("ticket", ticket).execute()

        prefix = f"#{ticket} 部分{REASON_LABEL[reason]}" if is_partial else f"#{ticket} {REASON_LABEL[reason]}"
        self._log(f"{prefix} {close_qty} {pos['symbol']} @ {close_price:.5f}，損益 {pnl:+.2f}")
        return {"ticket": ticket, "pnl": pnl}

    # ---------- views ----------

    def positions_view(self) -> list[dict]:
        return self._decorate_positions(self._positions_raw())

    def pending_view(self) -> list[dict]:
        return self.sb.table("pending_orders").select("*").execute().data

    def history_view(self, limit: int = 100) -> list[dict]:
        res = (self.sb.table("trade_history").select("*")
               .order("close_time", desc=True).limit(limit).execute())
        return res.data

    def journal_view(self, limit: int = 100) -> list[dict]:
        res = (self.sb.table("journal").select("*")
               .order("time", desc=True).limit(limit).execute())
        return res.data

    # ---------- tick processing: pending triggers, SL/TP, stop-out ----------

    def process_tick(self) -> dict:
        """Run one engine tick and return the state snapshot it already had to load.

        The snapshot is handed to the broadcaster so every connected client is
        served from these same queries — clients don't each poll the database.
        """
        pending = self.pending_view()
        opened = self._check_pending_triggers(pending)

        positions = self._positions_raw()
        closed = self._check_sl_tp(positions)

        # Only re-read after something actually changed the position set.
        if opened or closed:
            positions = self._positions_raw()
            pending = self.pending_view()

        fund = self._fund_row()
        stopped_out = self._check_stop_out(fund, positions)
        if stopped_out:
            positions = self._positions_raw()
            fund = self._fund_row()

        return {
            "account": self.fund_summary(fund, positions),
            "positions": self._decorate_positions(positions),
            "pending": pending,
            "changed": bool(opened or closed or stopped_out),
        }

    def _check_pending_triggers(self, orders: list[dict]) -> bool:
        triggered_any = False
        for order in orders:
            ba = self.hub.get_bid_ask(order["symbol"])
            if ba is None:
                continue
            bid, ask = ba
            side = "buy" if order["order_type"].startswith("buy") else "sell"
            triggered = (
                (order["order_type"] == "buy_limit" and ask <= order["trigger_price"]) or
                (order["order_type"] == "buy_stop" and ask >= order["trigger_price"]) or
                (order["order_type"] == "sell_limit" and bid >= order["trigger_price"]) or
                (order["order_type"] == "sell_stop" and bid <= order["trigger_price"])
            )
            if not triggered:
                continue

            fund = self._fund_row()
            entry_price = ask if side == "buy" else bid
            required_margin = order["qty"] * entry_price / fund["leverage"]
            summary = self.fund_summary(fund)
            ticket = order["ticket"]
            if required_margin > summary["free_margin"]:
                self.sb.table("pending_orders").delete().eq("ticket", ticket).execute()
                self._log(f"#{ticket} 掛單觸發失敗（保證金不足），已取消")
                triggered_any = True
                continue

            res = self.sb.table("positions").insert({
                "symbol": order["symbol"], "side": side, "qty": order["qty"],
                "entry_price": entry_price, "sl": order["sl"], "tp": order["tp"],
            }).execute()
            new_ticket = res.data[0]["ticket"]
            self.sb.table("pending_orders").delete().eq("ticket", ticket).execute()
            self._log(f"#{ticket} 掛單觸發 → #{new_ticket} {order['symbol']} @ {entry_price:.5f}")
            triggered_any = True
        return triggered_any

    def _check_sl_tp(self, positions: list[dict]) -> bool:
        closed_any = False
        for pos in positions:
            ba = self.hub.get_bid_ask(pos["symbol"])
            if ba is None:
                continue
            bid, ask = ba
            hit = None
            if pos["side"] == "buy":
                if pos["sl"] is not None and bid <= pos["sl"]:
                    hit = "sl"
                elif pos["tp"] is not None and bid >= pos["tp"]:
                    hit = "tp"
            else:
                if pos["sl"] is not None and ask >= pos["sl"]:
                    hit = "sl"
                elif pos["tp"] is not None and ask <= pos["tp"]:
                    hit = "tp"
            if hit:
                self._close(pos["ticket"], hit)
                closed_any = True
        return closed_any

    def _check_stop_out(self, fund: dict, positions: list[dict]) -> bool:
        summary = self.fund_summary(fund, positions)
        if summary["margin_used"] <= 0:
            return False
        if summary["margin_level"] is None or summary["margin_level"] >= STOP_OUT_LEVEL:
            return False

        remaining = sorted(positions, key=lambda p: self._position_floating(p))
        closed_any = False
        for pos in remaining:
            self._close(pos["ticket"], "stop_out")
            closed_any = True
            summary = self.fund_summary()
            if summary["margin_used"] <= 0 or (summary["margin_level"] or 999) >= STOP_OUT_LEVEL:
                break
        return closed_any

    def _decorate_positions(self, positions: list[dict]) -> list[dict]:
        return [{**pos, "floating_pnl": self._position_floating_or_none(pos)} for pos in positions]

    def _position_floating_or_none(self, pos: dict) -> float | None:
        return None if self.hub.get_bid_ask(pos["symbol"]) is None else self._position_floating(pos)

    def _position_floating(self, pos: dict) -> float:
        ba = self.hub.get_bid_ask(pos["symbol"])
        if ba is None:
            return 0.0
        bid, ask = ba
        exit_price = bid if pos["side"] == "buy" else ask
        sign = 1 if pos["side"] == "buy" else -1
        return (exit_price - pos["entry_price"]) * pos["qty"] * sign


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
