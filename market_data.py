import asyncio
import time
from abc import ABC, abstractmethod

import ccxt
import requests
import yfinance as yf

SPREAD_BPS = 5  # each side, in basis points (0.05%) -> ~0.10% total spread

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


class MarketDataProvider(ABC):
    """A source of live/delayed prices for a fixed set of symbols."""

    symbols: list[str] = []
    has_history = True  # False => hub builds candles from live ticks instead of asking the provider
    poll_interval = 5.0  # seconds between fetch() calls; providers with tight rate limits override this

    @abstractmethod
    def fetch(self) -> dict[str, float]:
        """Return {symbol: last_price} for every symbol this provider owns."""

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[dict]:
        """Return candle bars [{time, open, high, low, close}] oldest-first."""
        return []


class CryptoProvider(MarketDataProvider):
    """Real-time prices from Binance public market data (no API key needed).

    Accepts an optional symbol_map so internal symbol names (our own
    convention, e.g. "EURUSD=X") can be backed by a Binance spot market
    that isn't literally the same string (e.g. "EUR/USDT") — this is how
    EUR/GBP/AUD/gold ride on the same reliable feed as crypto instead of
    depending on a rate-limited forex API.
    """

    def __init__(self, symbols: list[str], symbol_map: dict[str, str] | None = None):
        self.symbols = symbols
        # Binance blocks US IPs (451 Unavailable For Legal Reasons), which is
        # where our host runs — Kraken serves the same public market data
        # without that restriction and carries all the pairs we need.
        self.exchange = ccxt.kraken()
        self.symbol_map = symbol_map or {}

    def _ex_symbol(self, symbol: str) -> str:
        return self.symbol_map.get(symbol, symbol)

    def fetch(self) -> dict[str, float]:
        prices = {}
        for symbol in self.symbols:
            try:
                ticker = self.exchange.fetch_ticker(self._ex_symbol(symbol))
                prices[symbol] = float(ticker["last"])
            except Exception as e:
                print(f"CryptoProvider.fetch({symbol}) failed: {type(e).__name__}: {e}", flush=True)
        return prices

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[dict]:
        tf = timeframe if timeframe in ("1m", "5m", "15m", "1h", "4h", "1d") else "1m"
        try:
            raw = self.exchange.fetch_ohlcv(self._ex_symbol(symbol), timeframe=tf, limit=limit)
        except Exception:
            return []
        return [
            {"time": int(t / 1000), "open": o, "high": h, "low": l, "close": c}
            for t, o, h, l, c, v in raw
        ]


class DelayedProvider(MarketDataProvider):
    """Delayed quotes for stocks/forex via yfinance (typically 15min delayed).

    Kept as a fallback for environments where Yahoo isn't rate-limited/blocked
    (this sandbox's outbound IP is; a normal machine usually isn't).
    """

    _INTERVAL_PERIOD = {
        "1m": "5d", "5m": "1mo", "15m": "1mo", "1h": "3mo", "1d": "2y",
    }

    def __init__(self, symbols: list[str]):
        self.symbols = symbols

    def fetch(self) -> dict[str, float]:
        prices = {}
        for symbol in self.symbols:
            try:
                fast_info = yf.Ticker(symbol).fast_info
                price = fast_info.get("lastPrice") or fast_info.get("last_price")
                if price:
                    prices[symbol] = float(price)
            except Exception:
                pass
        return prices

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[dict]:
        interval = timeframe if timeframe in self._INTERVAL_PERIOD else "1d"
        period = self._INTERVAL_PERIOD[interval]
        try:
            hist = yf.Ticker(symbol).history(period=period, interval=interval)
        except Exception:
            return []
        bars = [
            {
                "time": int(idx.timestamp()),
                "open": float(row.Open),
                "high": float(row.High),
                "low": float(row.Low),
                "close": float(row.Close),
            }
            for idx, row in hist.tail(limit).iterrows()
        ]
        return bars


class FinnhubProvider(MarketDataProvider):
    """Real-time US stock quotes via the Finnhub REST API (free tier).

    Free tier only covers the live /quote endpoint — historical candles are
    premium-gated, so has_history=False and the hub builds candles from ticks.
    """

    has_history = False

    def __init__(self, symbols: list[str], api_key: str):
        self.symbols = symbols
        self.api_key = api_key

    def fetch(self) -> dict[str, float]:
        prices = {}
        for symbol in self.symbols:
            try:
                r = requests.get(
                    "https://finnhub.io/api/v1/quote",
                    params={"symbol": symbol, "token": self.api_key}, timeout=5,
                )
                price = r.json().get("c")
                if price:
                    prices[symbol] = float(price)
            except Exception:
                pass
        return prices


class ForexProvider(MarketDataProvider):
    """Forex + gold spot rates via fxratesapi.com (free, no key, updates roughly every minute).

    Fetches every currency this basket needs relative to USD in a *single*
    request (the free tier rate-limits hard on a per-request basis), then
    derives each pair — including crosses and gold (XAU) — from that one
    USD-based rate table. No historical endpoint on the free tier, so
    has_history=False.
    """

    has_history = False
    poll_interval = 90.0  # free tier is ~61 requests/hour; one batched call per cycle keeps well under that

    def __init__(self, symbols: list[str]):
        self.symbols = symbols  # yfinance-style, e.g. "EURUSD=X", "XAUUSD=X"
        self._parsed = []
        needed = set()
        for symbol in symbols:
            pair = symbol[:-2] if symbol.endswith("=X") else symbol
            base, quote = pair[:3], pair[3:]
            self._parsed.append((symbol, base, quote))
            if base != "USD":
                needed.add(base)
            if quote != "USD":
                needed.add(quote)
        self._currencies = ",".join(sorted(needed))

    def fetch(self) -> dict[str, float]:
        prices = {}
        if not self._currencies:
            return prices
        try:
            r = requests.get(
                "https://api.fxratesapi.com/latest",
                params={"base": "USD", "currencies": self._currencies}, timeout=8,
            )
            rates = r.json().get("rates", {})
        except Exception:
            return prices
        rates["USD"] = 1.0
        for symbol, base, quote in self._parsed:
            try:
                if base == "USD":
                    price = rates[quote]
                elif quote == "USD":
                    price = 1.0 / rates[base]
                else:
                    price = rates[quote] / rates[base]
                prices[symbol] = price
            except (KeyError, ZeroDivisionError, TypeError):
                pass
        return prices


class CandleAggregator:
    """Builds OHLCV bars live from a stream of price ticks, per symbol/timeframe.

    Used for providers that only offer a live quote and no historical candles
    (Finnhub free tier, fxratesapi). Bars accumulate from whenever the server
    started, so history is empty until the app has been running a while.
    """

    def __init__(self, max_bars: int = 1000):
        self.max_bars = max_bars
        self._bars: dict[str, dict[str, list[dict]]] = {}

    def add_tick(self, symbol: str, price: float, ts: float):
        symbol_bars = self._bars.setdefault(symbol, {})
        for tf, seconds in TIMEFRAME_SECONDS.items():
            bucket_start = int(ts // seconds * seconds)
            bars = symbol_bars.setdefault(tf, [])
            if bars and bars[-1]["time"] == bucket_start:
                bar = bars[-1]
                bar["high"] = max(bar["high"], price)
                bar["low"] = min(bar["low"], price)
                bar["close"] = price
            else:
                bars.append({"time": bucket_start, "open": price, "high": price, "low": price, "close": price})
                if len(bars) > self.max_bars:
                    del bars[0]

    def get(self, symbol: str, timeframe: str, limit: int) -> list[dict]:
        bars = self._bars.get(symbol, {}).get(timeframe, [])
        return bars[-limit:]


class MarketDataHub:
    """Aggregates multiple providers into one live price cache.

    Each provider is polled on its own schedule (provider.poll_interval),
    since different free-tier APIs have very different rate limits.
    """

    def __init__(self, providers: list[MarketDataProvider]):
        self.providers = providers
        self.prices: dict[str, float] = {}
        self.last_updated: float = 0.0
        self.all_symbols = [s for p in providers for s in p.symbols]
        self.provider_by_symbol = {s: p for p in providers for s in p.symbols}
        self.aggregator = CandleAggregator()

    async def start(self):
        await asyncio.gather(*(self._poll_loop(p) for p in self.providers))

    async def _poll_loop(self, provider: MarketDataProvider):
        loop = asyncio.get_event_loop()
        while True:
            fetched = await loop.run_in_executor(None, provider.fetch)
            now = time.time()
            self.prices.update(fetched)
            if not provider.has_history:
                for symbol, price in fetched.items():
                    self.aggregator.add_tick(symbol, price, now)
            self.last_updated = now
            await asyncio.sleep(provider.poll_interval)

    def get_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)

    def get_bid_ask(self, symbol: str) -> tuple[float, float] | None:
        mid = self.prices.get(symbol)
        if mid is None:
            return None
        half = mid * (SPREAD_BPS / 10_000)
        return (mid - half, mid + half)

    def snapshot(self) -> dict[str, float]:
        return dict(self.prices)

    def bid_ask_snapshot(self) -> dict[str, dict]:
        out = {}
        for symbol in self.all_symbols:
            ba = self.get_bid_ask(symbol)
            if ba:
                out[symbol] = {"bid": ba[0], "ask": ba[1], "last": self.prices[symbol]}
        return out

    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[dict]:
        provider = self.provider_by_symbol.get(symbol)
        if provider is None:
            return []
        if not provider.has_history:
            return self.aggregator.get(symbol, timeframe, limit)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, provider.fetch_ohlcv, symbol, timeframe, limit)
