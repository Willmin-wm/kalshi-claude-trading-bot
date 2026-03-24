"""
Kalshi API Client — handles auth (RSA-PSS), market data, and order execution.
Adapted from ryanfrigo/kalshi-ai-trading-bot with streamlined async implementation.
"""

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.config import config

logger = logging.getLogger("kalshi")


class KalshiAPIError(Exception):
    pass


class KalshiClient:
    """Async Kalshi REST API client with RSA-PSS authentication."""

    def __init__(self):
        self.api_key = config.kalshi_api_key
        self.base_url = config.kalshi_base_url
        self.private_key = None
        self._load_private_key()
        # Use system SOCKS proxy if available, otherwise direct connection
        import os
        proxy_url = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy_url:
            # Convert socks5h:// to socks5:// (httpx/socksio doesn't support socks5h)
            proxy_url = proxy_url.replace("socks5h://", "socks5://")
        self.client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            proxy=proxy_url,
        )
        logger.info(f"KalshiClient initialized (base_url={self.base_url})")

    def _load_private_key(self):
        key_path = Path(config.kalshi_private_key_path)
        if not key_path.exists():
            raise KalshiAPIError(f"Private key not found: {key_path}")
        with open(key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)
        logger.info("RSA private key loaded")

    def _sign(self, timestamp: str, method: str, path: str) -> str:
        message = (timestamp + method.upper() + path).encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        auth: bool = True,
        retries: int = 5,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        if auth:
            ts = str(int(time.time() * 1000))
            headers["KALSHI-ACCESS-KEY"] = self.api_key
            headers["KALSHI-ACCESS-TIMESTAMP"] = ts
            headers["KALSHI-ACCESS-SIGNATURE"] = self._sign(ts, method, endpoint)

        body = json.dumps(json_data, separators=(",", ":")) if json_data else None
        if params:
            url = f"{url}?{urlencode(params)}"

        last_err = None
        for attempt in range(retries):
            try:
                await asyncio.sleep(0.5)  # Rate limit: max 2 req/s
                resp = await self.client.request(
                    method=method, url=url, headers=headers, content=body
                )
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response.status_code in (429, 500, 502, 503, 504):
                    wait = 0.5 * (2 ** attempt)
                    logger.warning(f"HTTP {e.response.status_code} on {endpoint}, retry in {wait:.1f}s")
                    await asyncio.sleep(wait)
                else:
                    raise KalshiAPIError(f"HTTP {e.response.status_code}: {e.response.text}")
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.5 * (2 ** attempt))
        raise KalshiAPIError(f"Failed after {retries} retries: {last_err}")

    # ── Account ──────────────────────────────────────────────────────────
    async def get_balance(self) -> Dict:
        return await self._request("GET", "/trade-api/v2/portfolio/balance")

    async def get_positions(self, **kwargs) -> Dict:
        params = {k: v for k, v in kwargs.items() if v is not None}
        return await self._request("GET", "/trade-api/v2/portfolio/positions", params=params or None)

    async def get_fills(self, ticker: str = None, limit: int = 100) -> Dict:
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return await self._request("GET", "/trade-api/v2/portfolio/fills", params=params)

    async def get_orders(self, ticker: str = None, status: str = None) -> Dict:
        params = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return await self._request("GET", "/trade-api/v2/portfolio/orders", params=params or None)

    # ── Markets ──────────────────────────────────────────────────────────
    async def get_markets(
        self,
        limit: int = 100,
        cursor: str = None,
        status: str = None,
        event_ticker: str = None,
        series_ticker: str = None,
    ) -> Dict:
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        return await self._request("GET", "/trade-api/v2/markets", params=params)

    async def get_market(self, ticker: str) -> Dict:
        return await self._request("GET", f"/trade-api/v2/markets/{ticker}", auth=False)

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Dict:
        return await self._request(
            "GET", f"/trade-api/v2/markets/{ticker}/orderbook",
            params={"depth": depth}, auth=False,
        )

    async def get_events(self, limit: int = 100, status: str = None, cursor: str = None) -> Dict:
        params = {"limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/trade-api/v2/events", params=params)

    # ── Trading ──────────────────────────────────────────────────────────
    async def place_order(
        self,
        ticker: str,
        client_order_id: str,
        side: str,
        action: str,
        count: int,
        type_: str = "market",
        yes_price: int = None,
        no_price: int = None,
    ) -> Dict:
        order = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side,
            "action": action,
            "count": count,
            "type": type_,
        }
        if yes_price is not None:
            order["yes_price"] = yes_price
        if no_price is not None:
            order["no_price"] = no_price
        return await self._request("POST", "/trade-api/v2/portfolio/orders", json_data=order)

    async def cancel_order(self, order_id: str) -> Dict:
        return await self._request("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}")

    # ── Cleanup ──────────────────────────────────────────────────────────
    async def close(self):
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
