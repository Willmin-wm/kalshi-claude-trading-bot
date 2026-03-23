# Kalshi Claude Trading Bot

An AI-powered prediction market trading bot for [Kalshi](https://kalshi.com) using Claude as the sole decision engine. Built with a Forecaster/Critic/Trader debate pattern, Kelly Criterion position sizing, and a real-time React dashboard.

Inspired by [ryanfrigo/kalshi-ai-trading-bot](https://github.com/ryanfrigo/kalshi-ai-trading-bot) â simplified from a 5-model ensemble to a single Claude-powered brain with cleaner architecture.

## Architecture

```
ââââââââââââââââââââââââââââââââââââââââââââââââ
â            React Dashboard (:8420)            â
â   Balance Â· P&L Â· Positions Â· Trades Â· AI    â
ââââââââââââââââââââ¬ââââââââââââââââââââââââââââ
                   â REST API (FastAPI)
ââââââââââââââââââââ´ââââââââââââââââââââââââââââ
â              Trading Engine                   â
â  ââââââââââââââââ  ââââââââââââââââââââââââ  â
â  â Kalshi API   â  â Claude Analyzer      â  â
â  â RSA-PSS Auth â  â Forecaster/Critic/   â  â
â  â Orders/Mkts  â  â Trader Debate        â  â
â  ââââââââââââââââ  ââââââââââââââââââââââââ  â
â  ââââââââââââââââââââââââââââââââââââââââââââ â
â  â Risk Manager                             â â
â  â Kelly Criterion Â· Drawdown Â· Sector Caps â â
â  ââââââââââââââââââââââââââââââââââââââââââââ â
â  ââââââââââââââââââââââââââââââââââââââââââââ â
â  â Position Tracker                         â â
â  â Stop-Loss Â· Take-Profit Â· Time Exits    â â
â  ââââââââââââââââââââââââââââââââââââââââââââ â
â  ââââââââââââââââââââââââââââââââââââââââââââ â
â  â SQLite Database                          â â
â  â Positions Â· Trades Â· Analyses Â· Stats    â â
â  ââââââââââââââââââââââââââââââââââââââââââââ â
ââââââââââââââââââââââââââââââââââââââââââââââââ
```

## How It Works

Each scan cycle (every 60 seconds):

1. **Fetch** â Pull open markets from Kalshi API, paginating up to 300 markets
2. **Filter** â Keep markets with volume > 200, YES price between 5Â¢â95Â¢, expiry < 14 days
3. **Analyze** â Send top 10 candidates (by volume) to Claude using a structured debate prompt:
   - **Forecaster**: estimates true YES probability from data, base rates, and reasoning
   - **Critic**: challenges assumptions, identifies biases and missing context
   - **Trader**: makes the final BUY/SKIP decision based on the debate
4. **Size** â Calculate position using fractional Kelly Criterion (quarter-Kelly)
5. **Execute** â Place limit orders on Kalshi via authenticated API
6. **Monitor** â Check positions every 30s for profit targets, stop losses, time exits, and settlements

## Risk Controls

| Control | Setting | Purpose |
|---------|---------|---------|
| Kelly Fraction | 0.25Ã | Quarter-Kelly prevents ruin from estimation error |
| Max Position | 3% of portfolio | No single bet can blow up the account |
| Min Confidence | 60% | Claude must be at least 60% confident |
| Min Edge | 5% | Our probability must beat market by 5%+ |
| Stop Loss | -15% | Auto-exit losing positions |
| Take Profit | +20% | Lock in gains |
| Max Hold | 10 days | Avoid capital decay |
| Max Positions | 10 | Diversification enforcement |
| Daily Loss Limit | 10% | Stop trading after a bad day |
| Max Drawdown | 15% | Circuit breaker from peak balance |
| Sector Cap | 30% | No concentrated category exposure |

## Quick Start

### Prerequisites

- Python 3.10+
- A [Kalshi](https://kalshi.com) account with API access (API key + RSA private key)
- An [Anthropic](https://console.anthropic.com) API key for Claude

### Setup

```bash
git clone https://github.com/YOUR_USERNAME/kalshi-claude-trading-bot.git
cd kalshi-claude-trading-bot

pip install -r requirements.txt

cp .env.example .env
# Edit .env with your API keys
```

### Configure API Keys

1. **Kalshi API Key** â Go to [Kalshi Settings â API](https://kalshi.com/account/settings), create a key, and download the RSA private key
2. **Save private key** as `kalshi_private_key.pem` in the project root
3. **Anthropic API Key** â Get one from [console.anthropic.com](https://console.anthropic.com)
4. **Edit `.env`** with your keys

### Run

```bash
# Test your Kalshi API connection
python run.py --test

# Check account status + positions
python run.py --status

# Run a single scan cycle (analyze + trade)
python run.py --scan

# Start the dashboard + auto-trading engine
python run.py
# Open http://localhost:8420
```

## Dashboard

The React dashboard at `http://localhost:8420` shows:

- **Balance & P&L** â Live account balance, daily P&L, unrealized gains
- **Open Positions** â All active trades with entry price, current price, confidence, unrealized P&L
- **Trade History** â Completed trades with entry/exit prices, P&L, and reasoning
- **AI Analyses** â Every market Claude analyzed with probability, edge, confidence, and decision
- **Live Markets** â Tradeable markets from Kalshi, sorted by volume
- **Engine Controls** â Start/stop the auto-trading loop or trigger manual scans

## Project Structure

```
âââ run.py                  # Entry point (--test, --scan, --status, or server)
âââ dashboard.html          # Single-file React dashboard
âââ requirements.txt        # Python dependencies
âââ .env.example            # Environment variable template
âââ src/
â   âââ config.py           # All settings in one place
â   âââ kalshi_client.py    # Kalshi REST API (RSA-PSS auth, orders, markets)
â   âââ claude_analyzer.py  # Claude market analysis (Forecaster/Critic/Trader)
â   âââ trading_engine.py   # Orchestrator (scan, size, execute, monitor)
â   âââ database.py         # SQLite persistence (positions, trades, analyses)
âââ api/
    âââ server.py           # FastAPI backend (12 endpoints + engine controls)
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard UI |
| `/api/dashboard` | GET | Full dashboard payload |
| `/api/balance` | GET | Live Kalshi balance |
| `/api/positions` | GET | Open positions from DB |
| `/api/trades` | GET | Trade history |
| `/api/analyses` | GET | Claude analysis log |
| `/api/stats` | GET | Win rate, P&L, trade count |
| `/api/markets` | GET | Filtered live markets |
| `/api/engine/start` | POST | Start auto-trading loop |
| `/api/engine/stop` | POST | Stop auto-trading loop |
| `/api/engine/scan` | POST | Trigger one manual scan |
| `/api/engine/status` | GET | Engine running state |

## Configuration

All settings live in `src/config.py`. Key parameters you might want to tune:

```python
min_confidence = 0.60    # Raise to 0.70+ for more conservative trading
min_edge = 0.05          # Raise to 0.10 for wider edge requirement
kelly_fraction = 0.25    # Lower = smaller bets, higher = more aggressive
max_position_pct = 0.03  # 3% max per trade
max_positions = 10       # Concurrent position limit
```

## Disclaimer

This is experimental software for educational purposes. Trading on prediction markets involves real financial risk. Past performance does not guarantee future results. Use at your own risk â start with paper trading (`LIVE_TRADING_ENABLED=false`) before risking real money.

## Credits

- Architecture inspired by [ryanfrigo/kalshi-ai-trading-bot](https://github.com/ryanfrigo/kalshi-ai-trading-bot)
- Powered by [Claude](https://anthropic.com) from Anthropic
- Built with [Kalshi API](https://trading-api.readme.io/reference/getting-started)

## License

MIT
