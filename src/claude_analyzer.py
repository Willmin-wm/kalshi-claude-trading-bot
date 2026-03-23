"""
Claude-powered market analyzer â replaces the 5-model ensemble with a single,
structured Claude analysis using the Forecaster/Critic/Trader debate pattern.

Returns structured JSON with: action, side, probability, confidence, reasoning.
"""

import json
import logging
import re
from typing import Any, Dict, Optional

import anthropic

from src.config import config

logger = logging.getLogger("claude_analyzer")

ANALYSIS_PROMPT = """You are an elite Kalshi prediction market trader. You combine three expert perspectives in one analysis:

1. **FORECASTER** â Estimate the true YES probability using all available data, base rates, and reasoning.
2. **CRITIC** â Challenge the forecast. Identify biases, missing context, and overconfidence.
3. **TRADER** â Make the final decision factoring in the forecaster's estimate, the critic's objections, and the market price.

## STRICT RULES
- You MUST output valid JSON and ONLY JSON. No text before or after.
- EV = (Your True Probability Ã 100) â Market Price. Only trade if |EV| â¥ {min_edge_pct}%.
- Be CALIBRATED: if you're 70% confident, you should be right 70% of the time. Avoid overconfidence.
- Consider base rates, historical precedent, current conditions, and timing.
- For sports: consider team records, injuries, home/away, recent performance, matchup history.
- For economics: consider consensus forecasts, recent data trends, Fed guidance, model estimates.
- For politics: consider polling averages, incumbency, approval ratings, historical patterns.
- NEVER trade just because a market exists. Most markets should be SKIP.

## MARKET DATA
- **Title:** {title}
- **Subtitle/Rules:** {subtitle}
- **YES Price:** {yes_price}Â¢ (market-implied probability: {yes_price}%)
- **NO Price:** {no_price}Â¢ (market-implied probability: {no_price}%)
- **Volume:** {volume:,} contracts
- **Open Interest:** {open_interest}
- **Days to Expiry:** {days_to_expiry}
- **Category:** {category}

## PORTFOLIO CONTEXT
- **Available Cash:** ${cash:.2f}
- **Max Position Size:** ${max_trade:.2f} ({max_pct}% of portfolio)
- **Open Positions:** {open_positions}

## ORDERBOOK
- **Best Bid (YES):** {best_bid}Â¢
- **Best Ask (YES):** {best_ask}Â¢
- **Spread:** {spread}Â¢

## OUTPUT FORMAT (strict JSON only)
{{
  "forecaster_probability": <float 0.0-1.0, your true YES probability>,
  "critic_objections": "<string: key risks and challenges to the forecast>",
  "action": "BUY" | "SKIP",
  "side": "YES" | "NO",
  "limit_price": <int 1-99, price in cents>,
  "confidence": <float 0.0-1.0, your certainty in this trade>,
  "edge": <float, your probability minus market probability>,
  "reasoning": "<string: 2-3 sentence explanation of why this trade has edge>"
}}

If SKIP, set side to "NONE", limit_price to 0, edge to 0.0.

Think step by step through forecaster â critic â trader, then output ONLY the JSON."""


class ClaudeAnalyzer:
    """Analyzes Kalshi markets using Claude for probability estimation and trade decisions."""

    def __init__(self):
        import os, httpx as _httpx
        # Handle SOCKS proxy environments
        proxy_url = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or os.environ.get("HTTPS_PROXY")
        http_client = None
        if proxy_url and "socks5h://" in proxy_url:
            proxy_url = proxy_url.replace("socks5h://", "socks5://")
            http_client = _httpx.Client(proxy=proxy_url, timeout=60.0)
        self.client = anthropic.Anthropic(
            api_key=config.anthropic_api_key,
            http_client=http_client,
        )
        self.model = config.claude_model
        self.total_cost = 0.0
        logger.info(f"ClaudeAnalyzer initialized (model={self.model})")

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate API cost based on Claude Sonnet 4 pricing."""
        # Sonnet 4: $3/M input, $15/M output
        return (input_tokens * 3.0 / 1_000_000) + (output_tokens * 15.0 / 1_000_000)

    def _extract_json(self, text: str) -> Optional[Dict]:
        """Extract JSON from Claude's response, handling markdown code blocks."""
        # Try direct JSON parse first
        text = text.strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass

        # Try extracting from code block
        patterns = [r"```json\s*(.*?)\s*```", r"```\s*(.*?)\s*```", r"(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})"]
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    continue
        return None

    async def analyze_market(
        self,
        market: Dict,
        orderbook: Dict,
        balance: float,
        open_positions: int,
    ) -> Optional[Dict]:
        """
        Analyze a single market and return a structured trading decision.

        Returns dict with: action, side, limit_price, confidence, edge, reasoning, probability
        Or None if analysis fails.
        """
        # Extract market data
        ticker = market.get("ticker", "")
        title = market.get("title", "Unknown")
        subtitle = market.get("subtitle", market.get("rules_primary", ""))
        yes_price = market.get("yes_price", 50) if market.get("yes_price") else 50
        no_price = market.get("no_price", 50) if market.get("no_price") else 50

        # Handle prices â Kalshi returns cents (1-99)
        # Some API responses might be in decimal (0.01-0.99)
        if isinstance(yes_price, float) and yes_price < 1:
            yes_price = int(yes_price * 100)
            no_price = int(no_price * 100)

        volume = market.get("volume", 0) or 0
        open_interest = market.get("open_interest", 0) or 0
        category = market.get("category", market.get("event_ticker", "unknown"))

        # Calculate days to expiry
        import time
        expiry_ts = market.get("expiration_time") or market.get("close_time")
        days_to_expiry = 0
        if expiry_ts:
            try:
                from datetime import datetime
                if isinstance(expiry_ts, str):
                    exp_dt = datetime.fromisoformat(expiry_ts.replace("Z", "+00:00"))
                    days_to_expiry = max(0, (exp_dt.timestamp() - time.time()) / 86400)
                else:
                    days_to_expiry = max(0, (expiry_ts - time.time()) / 86400)
            except Exception:
                days_to_expiry = 7  # default

        # Extract orderbook data
        best_bid = 0
        best_ask = 0
        if orderbook:
            yes_bids = orderbook.get("yes", []) if isinstance(orderbook.get("yes"), list) else []
            yes_asks = orderbook.get("no", []) if isinstance(orderbook.get("no"), list) else []
            # The orderbook structure varies â handle both formats
            if isinstance(orderbook.get("orderbook"), dict):
                ob = orderbook["orderbook"]
                yes_bids = ob.get("yes", [])
                yes_asks = ob.get("no", [])
            if yes_bids:
                best_bid = yes_bids[0][0] if isinstance(yes_bids[0], (list, tuple)) else yes_bids[0].get("price", 0)
            if yes_asks:
                best_ask = yes_asks[0][0] if isinstance(yes_asks[0], (list, tuple)) else yes_asks[0].get("price", 0)

        if best_bid == 0:
            best_bid = yes_price - 1
        if best_ask == 0:
            best_ask = yes_price + 1
        spread = best_ask - best_bid

        # Portfolio context
        max_trade = balance * config.max_position_pct
        max_pct = config.max_position_pct * 100

        prompt = ANALYSIS_PROMPT.format(
            min_edge_pct=int(config.min_edge * 100),
            title=title,
            subtitle=subtitle or "N/A",
            yes_price=yes_price,
            no_price=no_price,
            volume=volume,
            open_interest=open_interest,
            days_to_expiry=f"{days_to_expiry:.1f}",
            category=category,
            cash=balance,
            max_trade=max_trade,
            max_pct=max_pct,
            open_positions=open_positions,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
        )

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=config.claude_max_tokens,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )

            # Track cost
            usage = response.usage
            cost = self._estimate_cost(usage.input_tokens, usage.output_tokens)
            self.total_cost += cost

            # Parse response
            text = response.content[0].text
            result = self._extract_json(text)

            if not result:
                logger.warning(f"Failed to parse JSON for {ticker}: {text[:200]}")
                return None

            # Normalize the result
            result["ticker"] = ticker
            result["title"] = title
            result["yes_price"] = yes_price
            result["no_price"] = no_price
            result["volume"] = volume
            result["category"] = category
            result["cost_usd"] = cost
            result["raw_response"] = text[:500]

            action = result.get("action", "SKIP").upper()
            result["action"] = action
            result["decision"] = action

            # Validate
            if action == "BUY":
                side = result.get("side", "").upper()
                confidence = float(result.get("confidence", 0))
                edge = float(result.get("edge", 0))
                limit_price = int(result.get("limit_price", 0))

                if side not in ("YES", "NO"):
                    result["action"] = "SKIP"
                    result["reasoning"] = f"Invalid side: {side}"
                elif confidence < config.min_confidence:
                    result["action"] = "SKIP"
                    result["reasoning"] = f"Confidence {confidence:.0%} below minimum {config.min_confidence:.0%}"
                elif abs(edge) < config.min_edge:
                    result["action"] = "SKIP"
                    result["reasoning"] = f"Edge {edge:.1%} below minimum {config.min_edge:.1%}"
                elif limit_price < 1 or limit_price > 99:
                    result["action"] = "SKIP"
                    result["reasoning"] = f"Invalid limit price: {limit_price}"

            logger.info(
                f"Analysis: {ticker} â {result['action']} "
                f"(conf={result.get('confidence', 0):.0%}, edge={result.get('edge', 0):.1%}, "
                f"cost=${cost:.4f})"
            )
            return result

        except anthropic.APIError as e:
            logger.error(f"Claude API error for {ticker}: {e}")
            return None
        except Exception as e:
            logger.error(f"Analysis error for {ticker}: {e}", exc_info=True)
            return None
