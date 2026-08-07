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


# 야후 파이낸스가 기본 지원하지 않는 간격(3분/10분)은 더 잘게 받아와서 리샘플링한다.
# 분봉은 야후 정책상 최근 며칠~한두 달치만 제공되니 interval별로 조회 기간도 다르게 잡는다.
INTERVAL_CONFIG = {
    "1d": {"yf_interval": "1d", "period": "3y"},
    "1wk": {"yf_interval": "1wk", "period": "5y"},
    "1mo": {"yf_interval": "1mo", "period": "10y"},
    "1m": {"yf_interval": "1m", "period": "5d"},
    "3m": {"yf_interval": "1m", "period": "5d", "resample": "3min"},
    "5m": {"yf_interval": "5m", "period": "1mo"},
    "10m": {"yf_interval": "5m", "period": "1mo", "resample": "10min"},
    "30m": {"yf_interval": "30m", "period": "1mo"},
    "60m": {"yf_interval": "60m", "period": "3mo"},
}


@app.get("/api/chart/{code}")
def chart(code: str, interval: str = "1d"):
    cfg = INTERVAL_CONFIG.get(interval)
    if not cfg:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 interval입니다: {interval}")
    try:
        df = load_history(_yf_symbol(code), period=cfg["period"], interval=cfg["yf_interval"])
        if "resample" in cfg:
            df = (
                df.resample(cfg["resample"])
                .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
                .dropna()
            )
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))

    intraday = interval not in ("1d", "1wk", "1mo")
    return [
        {
            "time": int(row.Index.timestamp()) if intraday else row.Index.strftime("%Y-%m-%d"),
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
