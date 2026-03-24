"""
SQLite database for trade persistence, position tracking, and performance logging.
"""

import aiosqlite
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from src.config import config

logger = logging.getLogger("database")

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    title TEXT,
    side TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'buy',
    entry_price REAL NOT NULL,
    current_price REAL,
    quantity INTEGER NOT NULL,
    cost_basis REAL NOT NULL,
    confidence REAL,
    edge REAL,
    reasoning TEXT,
    category TEXT,
    strategy TEXT DEFAULT 'claude_directional',
    status TEXT DEFAULT 'open',
    stop_loss REAL,
    take_profit REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    pnl REAL DEFAULT 0.0,
    order_id TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    title TEXT,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    quantity INTEGER NOT NULL,
    pnl REAL DEFAULT 0.0,
    confidence REAL,
    reasoning TEXT,
    category TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    title TEXT,
    yes_price REAL,
    no_price REAL,
    volume INTEGER,
    claude_probability REAL,
    claude_confidence REAL,
    claude_side TEXT,
    claude_action TEXT,
    claude_reasoning TEXT,
    edge REAL,
    decision TEXT,
    cost_usd REAL DEFAULT 0.0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    starting_balance REAL,
    ending_balance REAL,
    trades_count INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    total_pnl REAL DEFAULT 0.0,
    ai_cost REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_analyses_ticker ON analyses(ticker);
CREATE INDEX IF NOT EXISTS idx_analyses_created ON analyses(created_at);
"""


class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.db_path

    async def initialize(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(DB_SCHEMA)
            await db.commit()
        logger.info(f"Database initialized: {self.db_path}")

    async def _execute(self, query: str, params: tuple = (), fetch: str = None):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            if fetch == "one":
                row = await cursor.fetchone()
                return dict(row) if row else None
            elif fetch == "all":
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
            else:
                await db.commit()
                return cursor.lastrowid

    # ── Positions ────────────────────────────────────────────────────────
    async def open_position(self, data: Dict) -> int:
        now = datetime.utcnow().isoformat()
        return await self._execute(
            """INSERT INTO positions
               (ticker, title, side, action, entry_price, current_price, quantity, cost_basis,
                confidence, edge, reasoning, category, strategy, status,
                stop_loss, take_profit, created_at, updated_at, order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["ticker"], data.get("title"), data["side"], data.get("action", "buy"),
                data["entry_price"], data["entry_price"], data["quantity"], data["cost_basis"],
                data.get("confidence"), data.get("edge"), data.get("reasoning"),
                data.get("category"), data.get("strategy", "claude_directional"), "open",
                data.get("stop_loss"), data.get("take_profit"), now, now,
                data.get("order_id"),
            ),
        )

    async def get_open_positions(self) -> List[Dict]:
        return await self._execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY created_at DESC",
            fetch="all",
        )

    async def get_position_by_ticker(self, ticker: str) -> Optional[Dict]:
        return await self._execute(
            "SELECT * FROM positions WHERE ticker = ? AND status = 'open'",
            (ticker,), fetch="one",
        )

    async def update_position(self, position_id: int, updates: Dict):
        sets = ", ".join(f"{k} = ?" for k in updates)
        updates["updated_at"] = datetime.utcnow().isoformat()
        sets += ", updated_at = ?"
        vals = list(updates.values()) + [position_id]
        await self._execute(f"UPDATE positions SET {sets} WHERE id = ?", tuple(vals))

    async def close_position(self, position_id: int, exit_price: float, pnl: float):
        now = datetime.utcnow().isoformat()
        await self._execute(
            """UPDATE positions SET status='closed', current_price=?, pnl=?,
               closed_at=?, updated_at=? WHERE id=?""",
            (exit_price, pnl, now, now, position_id),
        )

    # ── Trades ───────────────────────────────────────────────────────────
    async def log_trade(self, data: Dict) -> int:
        now = datetime.utcnow().isoformat()
        return await self._execute(
            """INSERT INTO trades
               (ticker, title, side, action, entry_price, exit_price, quantity,
                pnl, confidence, reasoning, category, created_at, closed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["ticker"], data.get("title"), data["side"], data["action"],
                data["entry_price"], data.get("exit_price"), data["quantity"],
                data.get("pnl", 0), data.get("confidence"), data.get("reasoning"),
                data.get("category"), now, data.get("closed_at"),
            ),
        )

    async def get_trades(self, limit: int = 50) -> List[Dict]:
        return await self._execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?",
            (limit,), fetch="all",
        )

    # ── Analyses ─────────────────────────────────────────────────────────
    async def log_analysis(self, data: Dict) -> int:
        now = datetime.utcnow().isoformat()
        return await self._execute(
            """INSERT INTO analyses
               (ticker, title, yes_price, no_price, volume,
                claude_probability, claude_confidence, claude_side, claude_action,
                claude_reasoning, edge, decision, cost_usd, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["ticker"], data.get("title"), data.get("yes_price"),
                data.get("no_price"), data.get("volume"),
                data.get("probability"), data.get("confidence"),
                data.get("side"), data.get("action"), data.get("reasoning"),
                data.get("edge"), data.get("decision"), data.get("cost_usd", 0), now,
            ),
        )

    async def get_recent_analyses(self, limit: int = 50) -> List[Dict]:
        return await self._execute(
            "SELECT * FROM analyses ORDER BY created_at DESC LIMIT ?",
            (limit,), fetch="all",
        )

    async def was_recently_analyzed(self, ticker: str, hours: int = 3) -> bool:
        row = await self._execute(
            """SELECT COUNT(*) as cnt FROM analyses
               WHERE ticker = ? AND created_at > datetime('now', ?)""",
            (ticker, f"-{hours} hours"), fetch="one",
        )
        return row and row["cnt"] > 0

    # ── Stats ────────────────────────────────────────────────────────────
    async def get_performance_stats(self) -> Dict:
        trades = await self._execute(
            "SELECT * FROM trades WHERE exit_price IS NOT NULL", fetch="all"
        )
        if not trades:
            return {"total_trades": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0}

        wins = [t for t in trades if t["pnl"] > 0]
        total_pnl = sum(t["pnl"] for t in trades)
        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(trades) - len(wins),
            "win_rate": len(wins) / len(trades) if trades else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(trades), 2) if trades else 0,
        }

    async def get_all_positions(self) -> List[Dict]:
        return await self._execute(
            "SELECT * FROM positions ORDER BY created_at DESC", fetch="all"
        )
