"""대시보드 백엔드. 실행: (trading-bot 폴더에서) python -m uvicorn webapp.server:app --reload"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backtest.engine import run_backtest as run_backtest_engine
from config import CONFIG, Config, RiskLimits, StrategyParams
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


def _resolve_symbol(code: str) -> str:
    """이미 야후 형식(.KS/.KQ, AAPL 등)이면 그대로, KRX 코드면 접미사를 붙여준다."""
    return code if "." in code or not code.isdigit() else _yf_symbol(code)


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


@app.get("/api/quote/{code}")
def quote(code: str):
    try:
        df = load_history(_yf_symbol(code), period="5d", interval="1d")
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    if len(df) == 0:
        raise HTTPException(status_code=404, detail="데이터 없음")
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    diff = float(last["Close"] - prev["Close"])
    pct = (diff / float(prev["Close"]) * 100) if prev["Close"] else 0.0
    return {"code": code, "price": float(last["Close"]), "diff": diff, "pct": round(pct, 2)}


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
    return db.list_trades_with_pnl()


@app.post("/api/trades")
def post_trades(item: TradeItem):
    if item.side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    db.add_trade(item.code, item.name, item.side, item.shares, item.price, item.note)
    return {"ok": True}


class OrderRequest(TradeItem):
    place_real_order: bool = True


@app.post("/api/order")
def place_order_api(req: OrderRequest):
    """매매일지에 기록하고, 요청하면 KIS 모의/실전 계좌에도 실제로 주문을 낸다.
    KIS 키가 없거나 주문이 실패해도 매매일지 기록 자체는 항상 남긴다."""
    if req.side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")

    broker_result = None
    broker_error = None
    if req.place_real_order:
        try:
            from broker.kis import KISBroker  # .env/자격증명 없이도 대시보드가 뜨도록 지연 임포트

            broker = KISBroker(CONFIG)
            broker_result = broker.place_order(req.code, req.side, req.shares)
        except Exception as e:
            broker_error = str(e)

    db.add_trade(req.code, req.name, req.side, req.shares, req.price, req.note)
    return {"ok": True, "broker_result": broker_result, "broker_error": broker_error}


@app.get("/api/holdings")
def get_holdings():
    return db.compute_holdings()


@app.get("/api/trades/stats")
def get_trade_stats():
    return db.compute_trade_stats()


class BacktestRequest(BaseModel):
    symbols: list[str]
    period: str = "3y"
    initial_capital: float = 10_000_000.0
    fast_ma: int = 20
    slow_ma: int = 60
    atr_period: int = 14
    atr_stop_multiple: float = 2.0
    risk_per_trade: float = 0.01
    max_position_weight: float = 0.2
    max_daily_loss: float = 0.03
    max_open_positions: int = 5


@app.post("/api/backtest")
def run_backtest_api(req: BacktestRequest):
    symbols = [s.strip() for s in req.symbols if s.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="종목을 1개 이상 입력하세요.")
    cfg = Config(
        symbols=[_resolve_symbol(s) for s in symbols],
        backtest_period=req.period,
        initial_capital=req.initial_capital,
        strategy=StrategyParams(
            fast_ma=req.fast_ma,
            slow_ma=req.slow_ma,
            atr_period=req.atr_period,
            atr_stop_multiple=req.atr_stop_multiple,
            risk_per_trade=req.risk_per_trade,
        ),
        risk=RiskLimits(
            max_position_weight=req.max_position_weight,
            max_daily_loss=req.max_daily_loss,
            max_open_positions=req.max_open_positions,
        ),
    )
    try:
        result = run_backtest_engine(cfg)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "metrics": result.metrics,
        "equity_curve": [{"date": str(d.date()), "equity": v} for d, v in result.equity_curve.items()],
        "trades": [
            {
                "symbol": t.symbol,
                "entry_date": str(t.entry_date.date()),
                "exit_date": str(t.exit_date.date()),
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "shares": t.shares,
                "pnl": t.pnl,
                "reason": t.reason,
            }
            for t in result.trades
        ],
    }
