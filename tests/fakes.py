"""In-memory stand-ins for the market feed and Supabase.

The engine's risky logic (PnL, spread, margin, SL/TP triggers) is what these
tests are for, and that logic needs exact, hand-picked prices to assert
against — a live feed can't give you "assert pnl == -25.0". So the feed is
driven by the test, and the store is a real (if small) implementation rather
than a mock that just echoes assumptions back.
"""

SPREAD_BPS = 5  # mirrors market_data.SPREAD_BPS


class FakeHub:
    """Market data with prices the test sets directly."""

    def __init__(self, prices: dict[str, float] | None = None):
        self.prices = dict(prices or {})
        self._forced: dict[str, tuple[float, float]] = {}

    def set(self, symbol: str, price: float):
        self.prices[symbol] = price
        self._forced.pop(symbol, None)

    def set_bid_ask(self, symbol: str, bid: float, ask: float):
        """Pin bid/ask exactly.

        Deriving them from a mid can't land a quote precisely on a trigger
        price — float rounding puts it just above or below — which is exactly
        the case a `<` vs `<=` bug hides in.
        """
        self._forced[symbol] = (bid, ask)
        self.prices[symbol] = (bid + ask) / 2

    def get_price(self, symbol):
        return self.prices.get(symbol)

    def get_bid_ask(self, symbol):
        if symbol in self._forced:
            return self._forced[symbol]
        mid = self.prices.get(symbol)
        if mid is None:
            return None
        half = mid * (SPREAD_BPS / 10_000)
        return (mid - half, mid + half)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Supports the chains engine.py actually uses."""

    def __init__(self, store, table):
        self.store, self.table = store, table
        self._op = None
        self._payload = None
        self._filters = []
        self._single = False
        self._limit = None
        self._order = None
        self._desc = False

    # -- builders --
    def select(self, *_):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def neq(self, col, val):
        self._filters.append(("!" + col, val))
        return self

    def single(self):
        self._single = True
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, col, desc=False):
        self._order, self._desc = col, desc
        return self

    # -- execution --
    def _matches(self, row):
        for col, val in self._filters:
            if col.startswith("!"):
                if row.get(col[1:]) == val:
                    return False
            elif row.get(col) != val:
                return False
        return True

    def execute(self):
        rows = self.store.tables.setdefault(self.table, [])

        if self._op == "insert":
            payload = self._payload if isinstance(self._payload, list) else [self._payload]
            created = []
            for item in payload:
                row = dict(item)
                row.setdefault("ticket", self.store.next_ticket())
                row.setdefault("id", len(rows) + 1)
                row.setdefault("open_time", "2026-01-01T00:00:00+00:00")
                rows.append(row)
                created.append(row)
            return _Result(created)

        matched = [r for r in rows if self._matches(r)]

        if self._op == "update":
            for row in matched:
                row.update(self._payload)
            return _Result(matched)

        if self._op == "delete":
            for row in matched:
                rows.remove(row)
            return _Result(matched)

        # select
        out = matched
        if self._order:
            out = sorted(out, key=lambda r: r.get(self._order) or "", reverse=self._desc)
        if self._limit is not None:
            out = out[: self._limit]
        if self._single:
            return _Result(out[0] if out else None)
        return _Result(out)


class FakeSupabase:
    def __init__(self, balance=100_000.0, leverage=100.0):
        self.tables = {
            "fund_account": [{"id": 1, "balance": balance, "leverage": leverage}],
            "positions": [],
            "pending_orders": [],
            "trade_history": [],
            "journal": [],
        }
        self._ticket = 1000

    def next_ticket(self):
        self._ticket += 1
        return self._ticket

    def table(self, name):
        return _Query(self, name)

    # -- helpers for assertions --
    @property
    def balance(self):
        return self.tables["fund_account"][0]["balance"]

    @property
    def positions(self):
        return self.tables["positions"]

    @property
    def history(self):
        return self.tables["trade_history"]
