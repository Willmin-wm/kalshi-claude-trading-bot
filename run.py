#!/usr/bin/env python3
"""
Kalshi Trading Bot â Main entry point.
Starts the FastAPI server with the embedded trading engine.

Usage:
  python run.py                  # Start dashboard + engine server
  python run.py --scan           # Run a single scan (no server)
  python run.py --status         # Check account status
  python run.py --test           # Test API connection only
"""

import argparse
import asyncio
import json
import logging
import os
import sys

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import config

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("main")


async def test_connection():
    """Test Kalshi API connection and display account info."""
    from src.kalshi_client import KalshiClient
    print("\n=== Kalshi API Connection Test ===")
    print(f"API Key: {config.kalshi_api_key[:8]}...{config.kalshi_api_key[-4:]}")
    print(f"Base URL: {config.kalshi_base_url}")
    print(f"Private Key: {config.kalshi_private_key_path}")

    async with KalshiClient() as client:
        # Test balance
        print("\n--- Account Balance ---")
        bal = await client.get_balance()
        balance_cents = bal.get("balance", 0)
        print(f"  Balance: ${balance_cents / 100:.2f}")
        print(f"  Raw response: {json.dumps(bal, indent=2)}")

        # Test positions
        print("\n--- Open Positions ---")
        pos = await client.get_positions()
        positions = pos.get("market_positions", pos.get("positions", []))
        if positions:
            for p in positions[:5]:
                print(f"  {p.get('ticker')}: {p.get('position')} contracts @ {p.get('market_exposure')}")
        else:
            print("  No open positions")

        # Test markets
        print("\n--- Sample Markets ---")
        mkts = await client.get_markets(limit=5, status="open")
        for m in mkts.get("markets", [])[:5]:
            print(f"  {m.get('ticker')}: {m.get('title', '')[:60]}... YES={m.get('yes_price')}Â¢")

        # Test fills
        print("\n--- Recent Fills ---")
        fills = await client.get_fills(limit=5)
        for f in fills.get("fills", [])[:5]:
            print(f"  {f.get('ticker')}: {f.get('side')} {f.get('count')}x @ {f.get('price')}Â¢")

    print("\nâ Connection successful!\n")


async def run_single_scan():
    """Run a single scan cycle."""
    from src.trading_engine import TradingEngine
    engine = TradingEngine()
    await engine.initialize()
    print(f"\nBalance: ${engine.balance:.2f}")
    print(f"Mode: {'LIVE' if config.live_trading else 'PAPER'}")
    print("Running scan...\n")
    result = await engine.scan_and_trade()
    print(json.dumps(result, indent=2, default=str))
    await engine.kalshi.close()


async def show_status():
    """Show current account and engine status."""
    from src.kalshi_client import KalshiClient
    from src.database import Database

    async with KalshiClient() as client:
        bal = await client.get_balance()
        print(f"\n=== Account Status ===")
        print(f"Balance: ${bal.get('balance', 0) / 100:.2f}")

        pos = await client.get_positions()
        positions = pos.get("market_positions", pos.get("positions", []))
        print(f"Open Positions: {len(positions)}")
        for p in positions:
            print(f"  {p.get('ticker')}: {p.get('position')} @ exposure={p.get('market_exposure')}")

    db = Database()
    await db.initialize()
    stats = await db.get_performance_stats()
    print(f"\n=== Trading Stats ===")
    print(f"Total Trades: {stats.get('total_trades', 0)}")
    print(f"Win Rate: {stats.get('win_rate', 0):.1%}")
    print(f"Total P&L: ${stats.get('total_pnl', 0):.2f}")
    print(f"Avg P&L/Trade: ${stats.get('avg_pnl', 0):.2f}")

    open_pos = await db.get_open_positions()
    print(f"\n=== DB Positions (Open) ===")
    for p in open_pos:
        print(f"  {p['ticker']} {p['side']} qty={p['quantity']} entry={p['entry_price']}Â¢")
    print()


def main():
    parser = argparse.ArgumentParser(description="Kalshi Trading Bot")
    parser.add_argument("--test", action="store_true", help="Test API connection")
    parser.add_argument("--scan", action="store_true", help="Run single scan")
    parser.add_argument("--status", action="store_true", help="Show account status")
    parser.add_argument("--port", type=int, default=config.api_port, help="Server port")
    args = parser.parse_args()

    if args.test:
        asyncio.run(test_connection())
    elif args.scan:
        asyncio.run(run_single_scan())
    elif args.status:
        asyncio.run(show_status())
    else:
        # Start the server
        import uvicorn
        print(f"""
âââââââââââââââââââââââââââââââââââââââââââââââââ
â     Kalshi Trading Dashboard                  â
â     Mode: {'LIVE ð´' if config.live_trading else 'PAPER ðµ'}                             â
â     Dashboard: http://localhost:{args.port}          â
â     API: http://localhost:{args.port}/api/dashboard   â
âââââââââââââââââââââââââââââââââââââââââââââââââ
        """)
        uvicorn.run(
            "api.server:app",
            host=config.api_host,
            port=args.port,
            reload=False,
            log_level="info",
        )


if __name__ == "__main__":
    main()
