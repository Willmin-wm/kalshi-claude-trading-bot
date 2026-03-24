"""
FastAPI server — serves dashboard data and provides control endpoints.
Runs the trading engine in the background.
"""

import asyncio
import logging
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from contextlib import asynccontextmanager

from src.config import config
from src.trading_engine import TradingEngine

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("server")

engine = TradingEngine()
engine_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine_task
    await engine.initialize()
    logger.info(f"Server starting — balance: ${engine.balance:.2f}")
    yield
    if engine_task:
        engine.running = False
        engine_task.cancel()
    await engine.kalshi.close()


app = FastAPI(title="Kalshi Trading Dashboard", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Dashboard ────────────────────────────────────────────────────────────

@app.get("/")
async def serve_dashboard():
    dashboard_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "dashboard.html",
    )
    if os.path.exists(dashboard_path):
        return FileResponse(dashboard_path, media_type="text/html")
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)


# ── API Endpoints ────────────────────────────────────────────────────────

@app.get("/api/dashboard")
async def get_dashboard():
    """Full dashboard payload — positions, trades, analyses, stats."""
    return await engine.get_dashboard_data()


@app.get("/api/balance")
async def get_balance():
    try:
        bal = await engine.kalshi.get_balance()
        return bal
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/positions")
async def get_positions():
    return await engine.db.get_open_positions()


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    return await engine.db.get_trades(limit)


@app.get("/api/analyses")
async def get_analyses(limit: int = 50):
    return await engine.db.get_recent_analyses(limit)


@app.get("/api/stats")
async def get_stats():
    return await engine.db.get_performance_stats()


@app.get("/api/markets")
async def get_markets(limit: int = 50):
    """Fetch live markets from Kalshi for display.

    Kalshi's get_markets list endpoint returns only MVE parlay markets.
    Real tradeable markets are nested under events, so we fetch events first
    then pull markets for each event.
    """
    def _vol(m):
        try: return float(m.get("volume_fp") or m.get("volume") or 0)
        except: return 0.0

    try:
        all_markets = []

        # Fetch open events and get their markets (real individual markets)
        events_result = await engine.kalshi.get_events(limit=20, status="open")
        events = events_result.get("events", [])
        for event in events:
            event_ticker = event.get("event_ticker")
            if not event_ticker:
                continue
            try:
                result = await engine.kalshi.get_markets(limit=20, event_ticker=event_ticker)
                all_markets.extend(result.get("markets", []))
            except Exception:
                continue

        all_markets.sort(key=_vol, reverse=True)
        return {"markets": all_markets[:limit], "total": len(all_markets)}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Engine Controls ──────────────────────────────────────────────────────

@app.post("/api/engine/start")
async def start_engine():
    global engine_task
    if engine.running:
        return {"status": "already_running"}
    engine_task = asyncio.create_task(engine.run())
    return {"status": "started", "mode": "LIVE" if config.live_trading else "PAPER"}


@app.post("/api/engine/stop")
async def stop_engine():
    global engine_task
    engine.running = False
    if engine_task:
        engine_task.cancel()
        engine_task = None
    return {"status": "stopped"}


@app.post("/api/engine/scan")
async def trigger_scan():
    """Trigger a single scan cycle manually."""
    if engine.running:
        return {"error": "Engine is running — stop it first to trigger manual scans"}
    result = await engine.scan_and_trade()
    return result


@app.get("/api/engine/status")
async def engine_status():
    return {
        "running": engine.running,
        "live_mode": config.live_trading,
        "balance": engine.balance,
        "peak_balance": engine.peak_balance,
        "daily_pnl": engine.daily_pnl,
        "scan_count": engine.scan_count,
        "last_scan": engine.last_scan_time,
        "ai_cost": engine.analyzer.total_cost,
    }


# ── Run ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.api_host, port=config.api_port)
