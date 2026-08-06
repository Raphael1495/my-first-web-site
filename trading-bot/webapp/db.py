import sqlite3
from datetime import datetime
from pathlib import Path

DB_FILE = Path(__file__).parent / "app.db"


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS watchlist (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            added_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            side TEXT NOT NULL,
            shares INTEGER NOT NULL,
            price REAL NOT NULL,
            note TEXT,
            traded_at TEXT NOT NULL
        )"""
    )
    conn.commit()
    conn.close()


def add_watchlist(code: str, name: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (code, name, added_at) VALUES (?, ?, ?)",
        (code, name, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def remove_watchlist(code: str):
    conn = get_conn()
    conn.execute("DELETE FROM watchlist WHERE code = ?", (code,))
    conn.commit()
    conn.close()


def list_watchlist() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_trade(code: str, name: str, side: str, shares: int, price: float, note: str = ""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO trades (code, name, side, shares, price, note, traded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (code, name, side, shares, price, note, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def list_trades() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM trades ORDER BY traded_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def compute_holdings() -> list:
    """매매일지에서 매수/매도를 순차 정산해 현재 보유 수량·평단가를 계산한다."""
    trades = sorted(list_trades(), key=lambda t: t["traded_at"])
    positions: dict[str, dict] = {}
    for t in trades:
        pos = positions.setdefault(t["code"], {"code": t["code"], "name": t["name"], "shares": 0, "avg_price": 0.0})
        if t["side"] == "buy":
            total_cost = pos["avg_price"] * pos["shares"] + t["price"] * t["shares"]
            pos["shares"] += t["shares"]
            pos["avg_price"] = total_cost / pos["shares"] if pos["shares"] > 0 else 0.0
        else:
            pos["shares"] -= t["shares"]
    return [p for p in positions.values() if p["shares"] > 0]
