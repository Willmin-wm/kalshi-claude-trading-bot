"""
Claude-powered market analyzer — replaces the 5-model ensemble with a single,
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

ANALYSIS_PROMPT = """You are running a structured debate between three expert personas to decide whether to trade a Kalshi prediction market contract. Work through each persona IN ORDER, then produce a final JSON decision.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MARKET DATA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Title: {title}
- Rules: {subtitle}
- YES Price: {yes_price}¢  (market-implied probability: {yes_price}%)
- NO Price: {no_price}¢
- Volume: {volume:,} contracts
- Open Interest: {open_interest}
- Days to Expiry: {days_to_expiry}
- Category: {category}

Orderbook — Best Bid: {best_bid}¢ | Best Ask: {best_ask}¢ | Spread: {spread}¢

Portfolio — Cash: ${cash:.2f} | Max Position: ${max_trade:.2f} ({max_pct}%) | Open Positions: {open_positions}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1 — FORECASTER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You are the Forecaster. Your job is to estimate the TRUE YES probability for this market.

Think through:
- What is the base rate for this type of event?
- What current data, trends, or signals shift the probability?
- For sports: team records, injuries, home/away, matchup history, recent form
- For economics: consensus forecasts, recent data prints, Fed signals, model estimates
- For politics: polling averages, incumbency, approval ratings, historical patterns
- For weather/science: model consensus, historical frequencies, current conditions
- What is your calibrated probability estimate? (If you'd say 70%, you should be right 70% of the time.)

State your TRUE YES probability as a decimal (0.0–1.0) with 2-3 sentences of reasoning.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 2 — CRITIC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You are the Critic. Your job is to CHALLENGE the Forecaster's estimate.

Think through:
- Is the Forecaster overconfident or anchored on a narrative?
- What information is MISSING that the market might be pricing in?
- Are there tail risks, black swan events, or regime changes being ignored?
- Is the sample size or base rate reliable, or could it be misleading?
- Would a well-informed counterparty disagree, and why?
- Should the probability be adjusted up or down?

State your key objections and whether the Forecaster's estimate should be revised.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 3 — TRADER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You are the Trader. Your job is to make the FINAL BUY or SKIP decision.

Rules:
- Compute edge = |your_probability − market_price/100|. Only BUY if edge ≥ {min_edge_pct}%.
- Factor in the Critic's objections — adjust the Forecaster's probability if warranted.
- Consider liquidity (spread, volume). Wide spreads eat into edge.
- Consider time to expiry — less time = less uncertainty = tighter edge needed.
- MOST markets should be SKIP. Only trade when you have a genuine informational or analytical edge.
- If BUY YES: your probability > market price → you think YES is underpriced.
- If BUY NO: your probability < market price → you think NO is underpriced.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT — JSON ONLY (no other text)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{{
  "forecaster_probability": <float 0.0-1.0, Forecaster's raw YES probability>,
  "forecaster_reasoning": "<string: Forecaster's 2-3 sentence rationale>",
  "critic_objections": "<string: Critic's key challenges and any probability adjustment>",
  "adjusted_probability": <float 0.0-1.0, post-Critic adjusted YES probability>,
  "action": "BUY" | "SKIP",
  "side": "YES" | "NO" | "NONE",
  "limit_price": <int 1-99, cents — 0 if SKIP>,
  "confidence": <float 0.0-1.0, Trader's overall certainty>,
  "edge": <float, adjusted_probability minus market_price/100 — 0.0 if SKIP>,
  "reasoning": "<string: Trader's 2-3 sentence final rationale incorporating the debate>"
}}

If SKIP: set side="NONE", limit_price=0, edge=0.0.

Now run the debate: Forecaster → Critic → Trader. Output ONLY the JSON."""


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

        # Kalshi v2 returns prices as strings (e.g. "0.6000") in dollars (0.0–1.0 range)
        # Fall back to legacy yes_price/yes_bid (cents) for compatibility
        def _safe_float(v):
            try: return float(v) if v is not None else 0.0
            except: return 0.0

        def _to_cents(dollar_fields, legacy_fields):
            for f in dollar_fields:
                v = _safe_float(market.get(f))
                if v > 0:
                    return int(v * 100)
            for f in legacy_fields:
                v = _safe_float(market.get(f))
                if v > 0:
                    return int(v * 100) if v < 1 else int(v)
            return 50  # default midpoint

        yes_price = _to_cents(
            ["yes_bid_dollars", "yes_ask_dollars", "last_price_dollars"],
            ["yes_price", "yes_bid"],
        )
        no_price = _to_cents(
            ["no_bid_dollars", "no_ask_dollars"],
            ["no_price", "no_bid"],
        )
        # Infer no_price from yes_price if missing
        if no_price == 50 and yes_price != 50:
            no_price = 100 - yes_price

        volume = _safe_float(market.get("volume_fp") or market.get("volume"))
        open_interest = _safe_float(market.get("open_interest_fp") or market.get("open_interest"))
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
            # The orderbook structure varies — handle both formats
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

            # Use adjusted_probability (post-Critic) if available, else fall back
            if "adjusted_probability" not in result:
                result["adjusted_probability"] = result.get("forecaster_probability", 0.5)

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
                f"Analysis: {ticker} → {result['action']} "
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
