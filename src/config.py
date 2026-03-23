"""
Configuration for Kalshi Trading System.
All settings in one place â tuned for disciplined live trading.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # === API Keys ===
    kalshi_api_key: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY", ""))
    kalshi_base_url: str = field(default_factory=lambda: os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com"))
    kalshi_private_key_path: str = field(default_factory=lambda: os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem"))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))

    # === Trading Mode ===
    live_trading: bool = field(default_factory=lambda: os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true")

    # === Market Filtering ===
    min_volume: int = 200              # Minimum contract volume to consider
    max_expiry_days: int = 14          # Max days until expiry
    min_yes_price: int = 5             # Min YES price in cents (avoid penny contracts)
    max_yes_price: int = 95            # Max YES price in cents (avoid near-certainties)

    # === AI Analysis ===
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_tokens: int = 4096
    min_confidence: float = 0.60       # Minimum confidence to trade (60%)
    min_edge: float = 0.05             # Minimum edge (5% = our prob - market prob)

    # === Position Sizing (Kelly Criterion) ===
    kelly_fraction: float = 0.25       # Quarter-Kelly for safety
    max_position_pct: float = 0.03     # Max 3% of portfolio per trade
    min_position_dollars: float = 5.0  # Minimum $5 position
    max_positions: int = 10            # Max concurrent positions

    # === Risk Management ===
    max_daily_loss_pct: float = 0.10   # Stop trading at 10% daily loss
    max_drawdown_pct: float = 0.15     # 15% max drawdown from peak
    max_sector_exposure: float = 0.30  # 30% max in any one category
    min_balance: float = 50.0          # Stop trading below $50

    # === Exit Strategy ===
    profit_target_pct: float = 0.20    # Take profit at +20%
    stop_loss_pct: float = 0.15        # Stop loss at -15%
    max_hold_hours: int = 240          # Max 10 days hold
    confidence_decay_exit: float = 0.25  # Exit if confidence drops 25%

    # === Scan Intervals ===
    scan_interval_seconds: int = 60    # Market scan every 60s
    position_check_seconds: int = 30   # Position check every 30s

    # === Server ===
    api_host: str = "0.0.0.0"
    api_port: int = 8420
    db_path: str = "trading.db"


config = Config()
