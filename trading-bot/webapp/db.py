import sqlite3
from datetime import datetime
from pathlib import Path

DB_FILE = Path(__file__).parent / "app.db"


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


DEFAULT_GROUP = "기본"


def init_db():
    conn = get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS watch_groups (
            name TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS watchlist (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            group_name TEXT NOT NULL DEFAULT '기본',
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
    # 기존 DB(그룹/해외 기능 이전에 만들어진)에는 컬럼이 없을 수 있어 보강한다.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(watchlist)")}
    if "group_name" not in existing_cols:
        conn.execute(f"ALTER TABLE watchlist ADD COLUMN group_name TEXT NOT NULL DEFAULT '{DEFAULT_GROUP}'")
    if "market" not in existing_cols:
        conn.execute("ALTER TABLE watchlist ADD COLUMN market TEXT NOT NULL DEFAULT 'domestic'")
    if "exchange" not in existing_cols:
        conn.execute("ALTER TABLE watchlist ADD COLUMN exchange TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO watch_groups (name, created_at) VALUES (?, ?)",
        (DEFAULT_GROUP, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def add_group(name: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO watch_groups (name, created_at) VALUES (?, ?)",
        (name, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def remove_group(name: str):
    if name == DEFAULT_GROUP:
        return  # 기본 그룹은 삭제 못 하게 막는다
    conn = get_conn()
    # 그룹을 지워도 종목은 안 지우고 기본 그룹으로 옮겨준다
    conn.execute("UPDATE watchlist SET group_name = ? WHERE group_name = ?", (DEFAULT_GROUP, name))
    conn.execute("DELETE FROM watch_groups WHERE name = ?", (name,))
    conn.commit()
    conn.close()


def list_groups() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM watch_groups ORDER BY created_at ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_watchlist(
    code: str, name: str, group_name: str = DEFAULT_GROUP, market: str = "domestic", exchange: str | None = None
):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (code, name, group_name, market, exchange, added_at) VALUES (?, ?, ?, ?, ?, ?)",
        (code, name, group_name, market, exchange, datetime.now().isoformat()),
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
    rows = conn.execute("SELECT * FROM watchlist ORDER BY group_name ASC, added_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def prune_watchlist(keep_codes: set):
    """keep_codes에 없는 관심종목은 전부 지운다. 자동매매가 사면 관심종목을 그 시점
    실제 보유종목 목록으로 동기화(=옛날에 넣어둔/이미 판 종목은 정리)하는 용도."""
    conn = get_conn()
    if keep_codes:
        placeholders = ",".join("?" for _ in keep_codes)
        conn.execute(f"DELETE FROM watchlist WHERE code NOT IN ({placeholders})", tuple(keep_codes))
    else:
        conn.execute("DELETE FROM watchlist")
    conn.commit()
    conn.close()


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


def delete_test_trades() -> int:
    """note가 'auto_test:'로 시작하는 테스트 매매기록을 전부 지운다 (실전/모의 실계좌 주문인
    'auto:live', 'auto:surge' 등은 절대 안 건드림). 보유종목/매매일지/종목별손익은 전부
    trades 테이블에서 계산되므로, 이 삭제 하나로 세 화면이 다 같이 정리된다."""
    conn = get_conn()
    cur = conn.execute("DELETE FROM trades WHERE note LIKE 'auto_test:%'")
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


def list_trades_with_pnl() -> list:
    """매매일지 표시용: 매도 행에는 그 시점 매수평단가와 등락률을 같이 계산해 붙여준다."""
    trades = sorted(list_trades(), key=lambda t: t["traded_at"])
    positions: dict[str, dict] = {}
    enriched = []
    for t in trades:
        pos = positions.setdefault(t["code"], {"shares": 0, "avg_price": 0.0})
        row = dict(t)
        if t["side"] == "buy":
            total_cost = pos["avg_price"] * pos["shares"] + t["price"] * t["shares"]
            pos["shares"] += t["shares"]
            pos["avg_price"] = total_cost / pos["shares"] if pos["shares"] > 0 else 0.0
            row["buy_avg_price"] = None
            row["pct_change"] = None
        else:
            buy_avg = pos["avg_price"]
            row["buy_avg_price"] = buy_avg if buy_avg else None
            row["pct_change"] = round((t["price"] - buy_avg) / buy_avg * 100, 2) if buy_avg else None
            pos["shares"] -= t["shares"]
        enriched.append(row)
    enriched.reverse()  # 최신순으로
    return enriched


def compute_holdings() -> list:
    """매매일지에서 매수/매도를 순차 정산해 현재 보유 수량·평단가를 계산한다.
    note는 가장 최근 매수 시점의 메모를 그대로 보여준다."""
    trades = sorted(list_trades(), key=lambda t: t["traded_at"])
    positions: dict[str, dict] = {}
    for t in trades:
        pos = positions.setdefault(
            t["code"], {"code": t["code"], "name": t["name"], "shares": 0, "avg_price": 0.0, "note": ""}
        )
        if t["side"] == "buy":
            total_cost = pos["avg_price"] * pos["shares"] + t["price"] * t["shares"]
            pos["shares"] += t["shares"]
            pos["avg_price"] = total_cost / pos["shares"] if pos["shares"] > 0 else 0.0
            pos["note"] = t["note"] or pos["note"]
        else:
            pos["shares"] -= t["shares"]
    return [p for p in positions.values() if p["shares"] > 0]


def compute_trade_stats(market: str = "all") -> dict:
    """평단가 기준으로 매도 시점마다 실현손익을 계산해 요약 통계를 낸다.
    market="domestic"/"overseas"로 필터링하면 그 시장 종목만으로 계산한다 — 국내(원화)와
    해외(달러) 손익을 그냥 합치면 통화가 섞여서 의미 없는 숫자가 되기 때문에 필요하다."""
    trades = sorted(list_trades(), key=lambda t: t["traded_at"])
    if market == "domestic":
        trades = [t for t in trades if t["code"].isdigit()]
    elif market == "overseas":
        trades = [t for t in trades if not t["code"].isdigit()]
    positions: dict[str, dict] = {}
    realized: list[dict] = []
    for t in trades:
        pos = positions.setdefault(t["code"], {"name": t["name"], "shares": 0, "avg_price": 0.0})
        if t["side"] == "buy":
            total_cost = pos["avg_price"] * pos["shares"] + t["price"] * t["shares"]
            pos["shares"] += t["shares"]
            pos["avg_price"] = total_cost / pos["shares"] if pos["shares"] > 0 else 0.0
        else:
            sell_shares = min(t["shares"], pos["shares"]) if pos["shares"] > 0 else 0
            cost = pos["avg_price"] * sell_shares
            pnl = (t["price"] - pos["avg_price"]) * sell_shares
            realized.append(
                {"code": t["code"], "name": t["name"], "traded_at": t["traded_at"], "shares": sell_shares,
                 "cost": cost, "pnl": pnl}
            )
            pos["shares"] -= t["shares"]

    total_pnl = sum(r["pnl"] for r in realized)
    wins = [r for r in realized if r["pnl"] > 0]
    win_rate = round(len(wins) / len(realized) * 100, 1) if realized else 0.0

    by_stock: dict[str, dict] = {}
    for r in realized:
        s = by_stock.setdefault(r["code"], {"code": r["code"], "name": r["name"], "pnl": 0.0, "cost": 0.0, "count": 0})
        s["pnl"] += r["pnl"]
        s["cost"] += r["cost"]
        s["count"] += 1
    for s in by_stock.values():
        s["pct"] = round(s["pnl"] / s["cost"] * 100, 2) if s["cost"] else 0.0
        del s["cost"]

    by_month: dict[str, float] = {}
    for r in realized:
        month = r["traded_at"][:7]
        by_month[month] = by_month.get(month, 0.0) + r["pnl"]

    return {
        "total_realized_pnl": total_pnl,
        "sell_count": len(realized),
        "win_rate": win_rate,
        "by_stock": sorted(by_stock.values(), key=lambda x: -abs(x["pnl"])),
        "by_month": [{"month": m, "pnl": p} for m, p in sorted(by_month.items())],
    }
