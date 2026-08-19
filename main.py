import asyncio
import datetime
import json
import os
import secrets
from pathlib import Path

import bcrypt
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
from supabase import Client, ClientOptions, create_client

from engine import TradingEngine
from market_data import CryptoProvider, DelayedProvider, FinnhubProvider, MarketDataHub

BASE_DIR = Path(__file__).parent
FINNHUB_KEY_FILE = BASE_DIR / "finnhub_key.txt"
SUPABASE_CONFIG_FILE = BASE_DIR / "supabase_config.json"
JWT_SECRET_FILE = BASE_DIR / "jwt_secret.txt"
SESSION_HOURS = 24 * 7  # a week — this is a shared login, not a personal account

CRYPTO_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
STOCK_SYMBOLS = ["AAPL", "TSLA", "NVDA"]

# EUR and gold are backed by real, liquid Binance spot markets (EUR/USDT,
# PAXG/USDT) — same reliable feed as crypto, no rate limits, real candles.
BINANCE_FOREX_MAP = {
    "EURUSD=X": "EUR/USDT",
    "XAUUSD=X": "PAXG/USDT",
}
FOREX_SYMBOLS = list(BINANCE_FOREX_MAP.keys())
DELAYED_SYMBOLS = STOCK_SYMBOLS + FOREX_SYMBOLS


def _jwt_secret() -> str:
    env_secret = os.environ.get("JWT_SECRET")
    if env_secret:
        return env_secret.strip()
    if JWT_SECRET_FILE.exists():
        return JWT_SECRET_FILE.read_text().strip()
    secret = secrets.token_hex(32)
    JWT_SECRET_FILE.write_text(secret)
    return secret


def _finnhub_key() -> str | None:
    key = os.environ.get("FINNHUB_API_KEY")
    if key:
        return key.strip()
    if FINNHUB_KEY_FILE.exists():
        key = FINNHUB_KEY_FILE.read_text().strip()
        return key or None
    return None


def _supabase_config() -> dict | None:
    url = os.environ.get("SUPABASE_URL")
    anon_key = os.environ.get("SUPABASE_ANON_KEY")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if url and anon_key and service_key:
        return {"url": url, "anon_key": anon_key, "service_key": service_key}
    if SUPABASE_CONFIG_FILE.exists():
        cfg = json.loads(SUPABASE_CONFIG_FILE.read_text())
        if cfg.get("url") and cfg.get("anon_key") and cfg.get("service_key"):
            return cfg
    return None


_fh_key = _finnhub_key()
stock_provider = FinnhubProvider(STOCK_SYMBOLS, _fh_key) if _fh_key else DelayedProvider(STOCK_SYMBOLS)

hub = MarketDataHub(
    providers=[
        CryptoProvider(CRYPTO_SYMBOLS),
        stock_provider,
        CryptoProvider(list(BINANCE_FOREX_MAP.keys()), symbol_map=BINANCE_FOREX_MAP),
    ],
)

sb_config = _supabase_config()
if sb_config is None:
    raise RuntimeError(
        "Supabase isn't configured yet. Create supabase_config.json with "
        '{"url": "...", "anon_key": "...", "service_key": "..."} '
        "(or set SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_SERVICE_KEY env vars)."
    )

# http2=False + a short keepalive_expiry avoid a known httpx/HTTP2 issue where
# pooled connections to Supabase go stale and raise RemoteProtocolError
# ("Server disconnected") under sustained polling traffic.
_httpx_client = httpx.Client(
    http2=False,
    limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=15.0),
    timeout=30.0,
)
sb: Client = create_client(
    sb_config["url"], sb_config["service_key"],
    options=ClientOptions(httpx_client=_httpx_client),
)
engine = TradingEngine(hub, sb)
JWT_SECRET = _jwt_secret()

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


connected_sockets: set[WebSocket] = set()


# ---------- auth ----------
#
# One shared account number, two passwords. Whichever password matches
# determines the role for that session — there's no per-person identity,
# just "the trading password" (admin) vs "the viewing password" (investor).

def _get_credentials() -> dict:
    res = sb.table("login_credentials").select("*").eq("id", 1).single().execute()
    if not res.data:
        raise HTTPException(500, "login not configured — insert a row into login_credentials")
    return res.data


def issue_token(role: str) -> str:
    payload = {
        "role": role,
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=SESSION_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


async def get_current_user(authorization: str | None = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid or expired token")
    return {"role": payload["role"]}


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "admin only")
    return user


# ---------- request models ----------

class LoginRequest(BaseModel):
    account_number: str
    password: str


class MarketOrderRequest(BaseModel):
    symbol: str
    side: str
    qty: float
    sl: float | None = None
    tp: float | None = None


class PendingOrderRequest(BaseModel):
    symbol: str
    order_type: str
    qty: float
    trigger_price: float
    sl: float | None = None
    tp: float | None = None


class CloseRequest(BaseModel):
    ticket: int
    qty: float | None = None


class ModifyRequest(BaseModel):
    ticket: int
    sl: float | None = None
    tp: float | None = None


class CancelRequest(BaseModel):
    ticket: int


# ---------- background loops ----------

@app.on_event("startup")
async def startup():
    asyncio.create_task(hub.start())
    asyncio.create_task(tick_loop())
    asyncio.create_task(broadcast_loop())


async def tick_loop():
    while True:
        await asyncio.sleep(2.0)
        try:
            await asyncio.to_thread(engine.process_tick)
        except Exception:
            pass


async def broadcast_loop():
    while True:
        await asyncio.sleep(2.0)
        if not connected_sockets:
            continue
        payload = json.dumps({"prices": hub.bid_ask_snapshot()})
        dead = set()
        for ws in connected_sockets:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        connected_sockets.difference_update(dead)


# ---------- routes ----------

@app.get("/")
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.post("/api/login")
async def login(req: LoginRequest):
    creds = await asyncio.to_thread(_get_credentials)
    if req.account_number != creds["account_number"]:
        raise HTTPException(401, "帳號或密碼錯誤")

    password_bytes = req.password.encode("utf-8")
    if bcrypt.checkpw(password_bytes, creds["admin_password_hash"].encode("utf-8")):
        role = "admin"
    elif bcrypt.checkpw(password_bytes, creds["investor_password_hash"].encode("utf-8")):
        role = "investor"
    else:
        raise HTTPException(401, "帳號或密碼錯誤")

    return {"token": issue_token(role), "role": role}


@app.get("/api/symbols")
async def symbols():
    return {"symbols": CRYPTO_SYMBOLS + DELAYED_SYMBOLS}


@app.get("/api/ohlcv/{symbol:path}")
async def ohlcv(symbol: str, timeframe: str = "1m", limit: int = 200):
    bars = await hub.get_ohlcv(symbol, timeframe, limit)
    return {"symbol": symbol, "timeframe": timeframe, "bars": bars}


@app.get("/api/me")
async def me(user: dict = Depends(get_current_user)):
    return {"role": user["role"]}


@app.get("/api/account")
async def account(user: dict = Depends(get_current_user)):
    return await asyncio.to_thread(engine.fund_summary)


@app.get("/api/positions")
async def positions(user: dict = Depends(get_current_user)):
    return {"positions": await asyncio.to_thread(engine.positions_view)}


@app.get("/api/pending")
async def pending(user: dict = Depends(get_current_user)):
    return {"pending": await asyncio.to_thread(engine.pending_view)}


@app.get("/api/history")
async def history(user: dict = Depends(get_current_user)):
    return {"history": await asyncio.to_thread(engine.history_view)}


@app.get("/api/journal")
async def journal(user: dict = Depends(get_current_user)):
    return {"journal": await asyncio.to_thread(engine.journal_view)}


@app.post("/api/order/market")
async def order_market(req: MarketOrderRequest, user: dict = Depends(require_admin)):
    try:
        pos = await asyncio.to_thread(engine.place_market_order, req.symbol, req.side, req.qty, req.sl, req.tp)
        return {"ok": True, "ticket": pos["ticket"]}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/order/pending")
async def order_pending(req: PendingOrderRequest, user: dict = Depends(require_admin)):
    try:
        order = await asyncio.to_thread(
            engine.place_pending_order, req.symbol, req.order_type, req.qty, req.trigger_price, req.sl, req.tp)
        return {"ok": True, "ticket": order["ticket"]}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/position/close")
async def position_close(req: CloseRequest, user: dict = Depends(require_admin)):
    try:
        trade = await asyncio.to_thread(engine.close_position, req.ticket, "manual", req.qty)
        return {"ok": True, "pnl": trade["pnl"]}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/position/modify")
async def position_modify(req: ModifyRequest, user: dict = Depends(require_admin)):
    try:
        await asyncio.to_thread(engine.modify_position, req.ticket, req.sl, req.tp)
        return {"ok": True}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/pending/cancel")
async def pending_cancel(req: CancelRequest, user: dict = Depends(require_admin)):
    try:
        await asyncio.to_thread(engine.cancel_pending, req.ticket)
        return {"ok": True}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_sockets.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected_sockets.discard(websocket)
