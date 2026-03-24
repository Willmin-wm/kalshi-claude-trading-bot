"""
Trading Engine — orchestrates market scanning, Claude analysis, order execution,
and position management with Kelly Criterion sizing and risk controls.
"""

import asyncio
import logging
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from src.config import config
from src.database import Database
from src.kalshi_client import KalshiClient, KalshiAPIError
from src.claude_analyzer import ClaudeAnalyzer

logger = logging.getLogger("engine")


class TradingEngine:
    """
    Core trading loop:
      1. Fetch markets from Kalshi
      2. Filter for tradeable opportunities
      3. Analyze with Claude (Forecaster/Critic/Trader)
      4. Size positions with Kelly Criterion
      5. Execute orders
      6. Monitor positions for exits
    """

    def __init__(self):
        self.kalshi = KalshiClient()
        self.analyzer = ClaudeAnalyzer()
        self.db = Database()
        self.running = False
        self.balance = 0.0
        self.peak_balance = 0.0
        self.daily_pnl = 0.0
        self.daily_start_balance = 0.0
        self.scan_count = 0
        self.last_scan_time = None
        self.last_scan_results = {}
        logger.info("TradingEngine initialized")

    async def initialize(self):
        """Initialize database and fetch initial balance."""
        await self.db.initialize()
        try:
            bal = await self.kalshi.get_balance()
            self.balance = bal.get("balance", 0) / 100  # Convert cents to dollars
            self.peak_balance = self.balance
            self.daily_start_balance = self.balance
            logger.info(f"Account balance: ${self.balance:.2f}")
        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            self.balance = 0

    # ── Kelly Criterion Position Sizing ───────────────────────────────────

    def kelly_size(
        self, probability: float, market_price_cents: int, balance: float
    ) -> Tuple[int, float]:
        """
        Calculate position size using fractional Kelly Criterion.

        For binary markets:
          Kelly % = (p × b - q) / b
          where p = win probability, q = 1-p, b = payout ratio

        Returns (quantity, dollar_amount).
        """
        if market_price_cents <= 0 or market_price_cents >= 100:
            return 0, 0.0

        price = market_price_cents / 100.0  # Convert to decimal
        p = probability  # Our estimated true probability
        q = 1.0 - p
        b = (1.0 - price) / price  # Payout ratio: win (1-price) / risk (price)

        # Kelly fraction
        kelly = (p * b - q) / b if b > 0 else 0
        kelly = max(0, kelly)  # Never negative

        # Apply fractional Kelly for safety
        fraction = kelly * config.kelly_fraction

        # Cap at max position percentage
        fraction = min(fraction, config.max_position_pct)

        # Dollar amount
        dollar_amount = balance * fraction
        dollar_amount = max(dollar_amount, 0)

        # Floor at minimum position size
        if dollar_amount < config.min_position_dollars:
            return 0, 0.0

        # Calculate quantity (contracts)
        quantity = int(dollar_amount / price)
        if quantity < 1:
            return 0, 0.0

        actual_cost = quantity * price
        return quantity, actual_cost

    # ── Market Filtering ─────────────────────────────────────────────────

    @staticmethod
    def _to_float(v) -> float:
        """Safely convert string/float/int/None to float."""
        try:
            return float(v) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    def _is_tradeable(self, market: Dict) -> bool:
        """Filter markets for tradeable opportunities."""
        status = market.get("status", "")
        if status not in ("open", "active"):
            return False

        # Price filters — Kalshi v2 API uses yes_bid_dollars (0.0–1.0 range) as strings
        # Fall back to legacy yes_price/yes_bid fields (cents)
        yes_price_dollars = next(
            (self._to_float(market.get(f)) for f in ("yes_bid_dollars", "yes_ask_dollars", "last_price_dollars")
             if self._to_float(market.get(f)) > 0),
            None
        )
        if yes_price_dollars is None:
            # Legacy format: yes_price/yes_bid in cents
            legacy = self._to_float(market.get("yes_price") or market.get("yes_bid"))
            if legacy <= 0:
                return False
            yes_price = int(legacy * 100) if legacy < 1 else int(legacy)
        else:
            yes_price = int(yes_price_dollars * 100)

        if yes_price < config.min_yes_price or yes_price > config.max_yes_price:
            return False

        # Volume filter — Kalshi v2 uses volume_fp (returned as string)
        volume = self._to_float(market.get("volume_fp") or market.get("volume"))
        if volume < config.min_volume:
            return False

        # Expiry filter
        expiry = market.get("expiration_time") or market.get("close_time")
        if expiry:
            try:
                if isinstance(expiry, str):
                    exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                else:
                    exp_dt = datetime.fromtimestamp(expiry, tz=timezone.utc)
                days_left = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 86400
                if days_left > config.max_expiry_days or days_left < 0:
                    return False
            except Exception:
                return False

        return True

    # ── Risk Checks ──────────────────────────────────────────────────────

    async def _check_risk_limits(self) -> bool:
        """Check portfolio-level risk limits. Returns True if trading is allowed."""
        # Minimum balance check
        if self.balance < config.min_balance:
            logger.warning(f"Balance ${self.balance:.2f} below minimum ${config.min_balance}")
            return False

        # Max positions check
        positions = await self.db.get_open_positions()
        if len(positions) >= config.max_positions:
            logger.warning(f"Max positions reached: {len(positions)}/{config.max_positions}")
            return False

        # Daily loss limit
        if self.daily_start_balance > 0:
            daily_loss = (self.daily_start_balance - self.balance) / self.daily_start_balance
            if daily_loss > config.max_daily_loss_pct:
                logger.warning(f"Daily loss limit hit: {daily_loss:.1%}")
                return False

        # Max drawdown from peak
        if self.peak_balance > 0:
            drawdown = (self.peak_balance - self.balance) / self.peak_balance
            if drawdown > config.max_drawdown_pct:
                logger.warning(f"Max drawdown hit: {drawdown:.1%}")
                return False

        return True

    # ── Order Execution ──────────────────────────────────────────────────

    async def _execute_trade(self, analysis: Dict, quantity: int, cost: float) -> Optional[str]:
        """Place a trade on Kalshi. Returns order_id or None."""
        ticker = analysis["ticker"]
        side = analysis["side"].lower()
        limit_price = int(analysis.get("limit_price", 0))

        if not config.live_trading:
            logger.info(f"[PAPER] Would buy {quantity}x {side} {ticker} @ {limit_price}¢")
            return f"paper-{uuid.uuid4().hex[:8]}"

        try:
            order_id = f"claude-{uuid.uuid4().hex[:12]}"
            result = await self.kalshi.place_order(
                ticker=ticker,
                client_order_id=order_id,
                side=side,
                action="buy",
                count=quantity,
                type_="limit",
                yes_price=limit_price if side == "yes" else None,
                no_price=limit_price if side == "no" else None,
            )

            actual_order_id = result.get("order", {}).get("order_id", order_id)
            logger.info(
                f"ORDER PLACED: {quantity}x {side} {ticker} @ {limit_price}¢ "
                f"(order_id={actual_order_id}, cost=${cost:.2f})"
            )
            return actual_order_id

        except KalshiAPIError as e:
            logger.error(f"Order failed for {ticker}: {e}")
            return None

    # ── Position Monitoring ──────────────────────────────────────────────

    async def check_positions(self):
        """Monitor open positions and execute exit strategies."""
        positions = await self.db.get_open_positions()
        if not positions:
            return

        for pos in positions:
            try:
                ticker = pos["ticker"]

                # Get current market price
                try:
                    market = await self.kalshi.get_market(ticker)
                    market_data = market.get("market", market)
                except Exception:
                    continue

                side = pos["side"].upper()
                entry_price = pos["entry_price"]

                # Current price based on our side
                if side == "YES":
                    current = (market_data.get("yes_price") or market_data.get("yes_bid") or entry_price)
                else:
                    current = (market_data.get("no_price") or market_data.get("no_bid") or entry_price)

                # Handle decimal vs cents
                if isinstance(current, float) and current < 1:
                    current = current * 100

                # Update current price in DB
                await self.db.update_position(pos["id"], {"current_price": current})

                # Check exit conditions
                if entry_price > 0:
                    pnl_pct = (current - entry_price) / entry_price

                    # Profit target
                    if pnl_pct >= config.profit_target_pct:
                        await self._exit_position(pos, current, "PROFIT_TARGET")
                        continue

                    # Stop loss
                    if pnl_pct <= -config.stop_loss_pct:
                        await self._exit_position(pos, current, "STOP_LOSS")
                        continue

                # Time-based exit
                created = datetime.fromisoformat(pos["created_at"])
                hours_held = (datetime.utcnow() - created).total_seconds() / 3600
                if hours_held > config.max_hold_hours:
                    await self._exit_position(pos, current, "TIME_EXIT")
                    continue

                # Check if market is settled
                status = market_data.get("status", "")
                if status in ("settled", "closed", "finalized"):
                    result = market_data.get("result", "")
                    if result:
                        exit_price = 100 if result.upper() == side else 0
                        await self._exit_position(pos, exit_price, f"SETTLED_{result.upper()}")

            except Exception as e:
                logger.error(f"Error checking position {pos.get('ticker')}: {e}")

    async def _exit_position(self, pos: Dict, exit_price: float, reason: str):
        """Close a position and log the trade."""
        entry = pos["entry_price"]
        quantity = pos["quantity"]
        pnl = (exit_price - entry) * quantity / 100  # Convert cents to dollars

        logger.info(
            f"EXIT [{reason}]: {pos['ticker']} {pos['side']} "
            f"entry={entry}¢ exit={exit_price}¢ qty={quantity} pnl=${pnl:.2f}"
        )

        # Close in DB
        await self.db.close_position(pos["id"], exit_price, pnl)

        # Log trade
        await self.db.log_trade({
            "ticker": pos["ticker"],
            "title": pos.get("title"),
            "side": pos["side"],
            "action": "sell",
            "entry_price": entry,
            "exit_price": exit_price,
            "quantity": quantity,
            "pnl": pnl,
            "confidence": pos.get("confidence"),
            "reasoning": f"Exit: {reason}",
            "category": pos.get("category"),
            "closed_at": datetime.utcnow().isoformat(),
        })

        # Place sell order on Kalshi (if live and not settled)
        if config.live_trading and reason not in ("SETTLED_YES", "SETTLED_NO"):
            try:
                order_id = f"exit-{uuid.uuid4().hex[:12]}"
                await self.kalshi.place_order(
                    ticker=pos["ticker"],
                    client_order_id=order_id,
                    side=pos["side"].lower(),
                    action="sell",
                    count=quantity,
                    type_="market",
                )
            except Exception as e:
                logger.error(f"Failed to place exit order: {e}")

        self.daily_pnl += pnl

    # ── Main Scan Loop ───────────────────────────────────────────────────

    async def scan_and_trade(self) -> Dict:
        """
        Run one full scan cycle:
        1. Fetch markets
        2. Filter for opportunities
        3. Analyze top candidates with Claude
        4. Execute trades
        5. Check existing positions

        Returns summary dict for the dashboard.
        """
        self.scan_count += 1
        self.last_scan_time = datetime.utcnow().isoformat()
        summary = {
            "scan_number": self.scan_count,
            "timestamp": self.last_scan_time,
            "markets_fetched": 0,
            "markets_filtered": 0,
            "markets_analyzed": 0,
            "trades_executed": 0,
            "errors": [],
        }

        try:
            # Refresh balance
            try:
                bal = await self.kalshi.get_balance()
                self.balance = bal.get("balance", 0) / 100
                self.peak_balance = max(self.peak_balance, self.balance)
            except Exception as e:
                summary["errors"].append(f"Balance fetch: {e}")

            # Risk check
            if not await self._check_risk_limits():
                summary["errors"].append("Risk limits breached — skipping scan")
                return summary

            # Check existing positions first
            await self.check_positions()

            # Fetch markets via events (Kalshi v2: get_markets list only returns
            # MVE parlays; real tradeable markets are nested under events)
            all_markets = []
            try:
                events_result = await self.kalshi.get_events(limit=50, status="open")
                events = events_result.get("events", [])
                for event in events:
                    event_ticker = event.get("event_ticker")
                    if not event_ticker:
                        continue
                    try:
                        result = await self.kalshi.get_markets(
                            limit=100, event_ticker=event_ticker
                        )
                        all_markets.extend(result.get("markets", []))
                    except Exception as e:
                        summary["errors"].append(f"Market fetch {event_ticker}: {e}")
            except Exception as e:
                summary["errors"].append(f"Events fetch: {e}")

            summary["markets_fetched"] = len(all_markets)

            # Filter
            candidates = [m for m in all_markets if self._is_tradeable(m)]
            summary["markets_filtered"] = len(candidates)

            if not candidates:
                logger.info("No tradeable markets found this scan")
                return summary

            # Sort by volume (highest first) and take top N
            candidates.sort(key=lambda m: self._to_float(m.get("volume_fp") or m.get("volume")), reverse=True)
            to_analyze = candidates[:10]  # Analyze top 10 by volume

            # Analyze with Claude
            open_positions = await self.db.get_open_positions()
            existing_tickers = {p["ticker"] for p in open_positions}

            for market in to_analyze:
                ticker = market.get("ticker", "")

                # Skip if we already have a position
                if ticker in existing_tickers:
                    continue

                # Skip if recently analyzed
                if await self.db.was_recently_analyzed(ticker, hours=3):
                    continue

                # Get orderbook
                try:
                    orderbook = await self.kalshi.get_orderbook(ticker)
                except Exception:
                    orderbook = {}

                # Claude analysis
                analysis = await self.analyzer.analyze_market(
                    market=market,
                    orderbook=orderbook,
                    balance=self.balance,
                    open_positions=len(open_positions),
                )

                if not analysis:
                    continue

                summary["markets_analyzed"] += 1

                # Log analysis
                # Normalize prices for DB logging (v2 API uses _dollars fields as strings)
                def _price_cents(d_field, l_field):
                    v = self._to_float(market.get(d_field) or market.get(l_field))
                    if v <= 0:
                        return 0
                    return int(v * 100) if v < 1 else int(v)
                _yp = _price_cents("yes_bid_dollars", "yes_price")
                _np = _price_cents("no_bid_dollars", "no_price")
                _vol = self._to_float(market.get("volume_fp") or market.get("volume"))
                await self.db.log_analysis({
                    "ticker": ticker,
                    "title": market.get("title"),
                    "yes_price": _yp,
                    "no_price": _np,
                    "volume": _vol,
                    "probability": analysis.get("forecaster_probability"),
                    "confidence": analysis.get("confidence"),
                    "side": analysis.get("side"),
                    "action": analysis.get("action"),
                    "reasoning": analysis.get("reasoning"),
                    "edge": analysis.get("edge"),
                    "decision": analysis.get("action"),
                    "cost_usd": analysis.get("cost_usd", 0),
                })

                # Execute if BUY
                if analysis.get("action") == "BUY":
                    side = analysis["side"].upper()
                    probability = float(analysis.get("forecaster_probability", 0.5))
                    limit_price = int(analysis.get("limit_price", 0))

                    # Kelly sizing
                    if side == "YES":
                        quantity, cost = self.kelly_size(probability, limit_price, self.balance)
                    else:
                        # For NO side, flip the probability
                        quantity, cost = self.kelly_size(1 - probability, 100 - limit_price, self.balance)

                    if quantity > 0 and cost > 0:
                        order_id = await self._execute_trade(analysis, quantity, cost)

                        if order_id:
                            # Calculate stop/take profit levels
                            entry = limit_price
                            stop_loss = entry * (1 - config.stop_loss_pct)
                            take_profit = entry * (1 + config.profit_target_pct)

                            await self.db.open_position({
                                "ticker": ticker,
                                "title": market.get("title"),
                                "side": side,
                                "action": "buy",
                                "entry_price": entry,
                                "quantity": quantity,
                                "cost_basis": cost,
                                "confidence": analysis.get("confidence"),
                                "edge": analysis.get("edge"),
                                "reasoning": analysis.get("reasoning"),
                                "category": analysis.get("category"),
                                "strategy": "claude_directional",
                                "stop_loss": stop_loss,
                                "take_profit": take_profit,
                                "order_id": order_id,
                            })

                            await self.db.log_trade({
                                "ticker": ticker,
                                "title": market.get("title"),
                                "side": side,
                                "action": "buy",
                                "entry_price": entry,
                                "quantity": quantity,
                                "confidence": analysis.get("confidence"),
                                "reasoning": analysis.get("reasoning"),
                                "category": analysis.get("category"),
                            })

                            summary["trades_executed"] += 1
                            logger.info(
                                f"TRADE: BUY {quantity}x {side} {ticker} @ {limit_price}¢ "
                                f"(cost=${cost:.2f}, confidence={analysis.get('confidence', 0):.0%})"
                            )

        except Exception as e:
            logger.error(f"Scan error: {e}", exc_info=True)
            summary["errors"].append(str(e))

        self.last_scan_results = summary
        return summary

    # ── Trading Loop ─────────────────────────────────────────────────────

    async def run(self):
        """Main trading loop — runs continuously."""
        self.running = True
        logger.info("=== TRADING ENGINE STARTED ===")
        logger.info(f"Mode: {'LIVE' if config.live_trading else 'PAPER'}")
        logger.info(f"Balance: ${self.balance:.2f}")

        while self.running:
            try:
                summary = await self.scan_and_trade()
                logger.info(
                    f"Scan #{summary['scan_number']}: "
                    f"fetched={summary['markets_fetched']}, "
                    f"filtered={summary['markets_filtered']}, "
                    f"analyzed={summary['markets_analyzed']}, "
                    f"traded={summary['trades_executed']}"
                )
            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)

            await asyncio.sleep(config.scan_interval_seconds)

    async def stop(self):
        """Stop the trading loop."""
        self.running = False
        await self.kalshi.close()
        logger.info("Trading engine stopped")

    # ── Dashboard Data ───────────────────────────────────────────────────

    async def get_dashboard_data(self) -> Dict:
        """Return all data needed for the dashboard."""
        positions = await self.db.get_open_positions()
        trades = await self.db.get_trades(limit=100)
        analyses = await self.db.get_recent_analyses(limit=50)
        stats = await self.db.get_performance_stats()

        # Calculate position P&L
        total_unrealized = 0
        for p in positions:
            if p.get("current_price") and p.get("entry_price"):
                pnl = (p["current_price"] - p["entry_price"]) * p["quantity"] / 100
                p["unrealized_pnl"] = round(pnl, 2)
                total_unrealized += pnl
            else:
                p["unrealized_pnl"] = 0

        return {
            "balance": self.balance,
            "peak_balance": self.peak_balance,
            "daily_pnl": round(self.daily_pnl, 2),
            "total_unrealized_pnl": round(total_unrealized, 2),
            "ai_cost_today": round(self.analyzer.total_cost, 4),
            "positions": positions,
            "trades": trades,
            "analyses": analyses,
            "stats": stats,
            "engine": {
                "running": self.running,
                "live_mode": config.live_trading,
                "scan_count": self.scan_count,
                "last_scan": self.last_scan_time,
                "last_scan_results": self.last_scan_results,
            },
            "config": {
                "min_confidence": config.min_confidence,
                "min_edge": config.min_edge,
                "kelly_fraction": config.kelly_fraction,
                "max_position_pct": config.max_position_pct,
                "max_positions": config.max_positions,
                "profit_target": config.profit_target_pct,
                "stop_loss": config.stop_loss_pct,
            },
        }
