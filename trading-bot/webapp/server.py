"""대시보드 백엔드. 실행: (trading-bot 폴더에서) python -m uvicorn webapp.server:app --reload"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from data.loader import load_history

from . import db
from .krx_symbols import load_symbols, search_symbols

app = FastAPI(title="주식 자동매매 대시보드")
db.init_db()

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _yf_symbol(code: str) -> str:
    symbols = {s["code"]: s for s in load_symbols()}
    market = symbols.get(code, {}).get("market", "KOSPI")
    suffix = ".KQ" if market == "KOSDAQ" else ".KS"
    return f"{code}{suffix}"


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/search")
def search(q: str):
    return search_symbols(q)


@app.get("/api/chart/{code}")
def chart(code: str, period: str = "1y"):
    try:
        df = load_history(_yf_symbol(code), period=period)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    return [
        {
            "time": row.Index.strftime("%Y-%m-%d"),
            "open": row.Open,
            "high": row.High,
            "low": row.Low,
            "close": row.Close,
        }
        for row in df.itertuples()
    ]


class WatchlistItem(BaseModel):
    code: str
    name: str
    group_name: str = db.DEFAULT_GROUP


@app.get("/api/watchlist")
def get_watchlist():
    return db.list_watchlist()


@app.post("/api/watchlist")
def post_watchlist(item: WatchlistItem):
    db.add_watchlist(item.code, item.name, item.group_name)
    return {"ok": True}


@app.delete("/api/watchlist/{code}")
def delete_watchlist(code: str):
    db.remove_watchlist(code)
    return {"ok": True}


class GroupItem(BaseModel):
    name: str


@app.get("/api/groups")
def get_groups():
    return db.list_groups()


@app.post("/api/groups")
def post_groups(item: GroupItem):
    name = item.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="그룹 이름을 입력하세요.")
    db.add_group(name)
    return {"ok": True}


@app.delete("/api/groups/{name}")
def delete_group(name: str):
    db.remove_group(name)
    return {"ok": True}


class TradeItem(BaseModel):
    code: str
    name: str
    side: str
    shares: int
    price: float
    note: str = ""


@app.get("/api/trades")
def get_trades():
    return db.list_trades()


@app.post("/api/trades")
def post_trades(item: TradeItem):
    if item.side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    db.add_trade(item.code, item.name, item.side, item.shares, item.price, item.note)
    return {"ok": True}


@app.get("/api/holdings")
def get_holdings():
    return db.compute_holdings()
