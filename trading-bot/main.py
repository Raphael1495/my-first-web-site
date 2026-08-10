import argparse
import json
import sys
from datetime import date
from pathlib import Path

from backtest.engine import run_backtest
from config import CONFIG
from data.loader import load_history
from strategy.trend_following import compute_indicators, position_size, stop_price

STATE_FILE = Path(__file__).parent / ".state.json"


def cmd_backtest():
    result = run_backtest(CONFIG)
    print("\n=== 백테스트 결과 ===")
    for k, v in result.metrics.items():
        print(f"{k}: {v}")
    print(f"\n최근 거래 5건:")
    for t in result.trades[-5:]:
        print(f"  {t.symbol} {t.entry_date.date()}~{t.exit_date.date()} "
              f"{t.shares}주 손익 {t.pnl:,.0f} ({t.reason})")
    out_csv = Path(__file__).parent / "backtest_equity_curve.csv"
    result.equity_curve.to_csv(out_csv, header=["equity"])
    print(f"\n일별 자산 곡선 저장: {out_csv}")


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))


def _process_domestic(broker, symbol: str, balance: dict, equity: float, open_count: int, max_positions: int) -> int:
    """반환값: 신규 진입하면 +1, 청산하면 -1, 아무 일도 없으면 0 (동시보유 종목수 갱신용)."""
    kis_code = symbol.split(".")[0]
    hist = compute_indicators(load_history(symbol, period="1y"), CONFIG.strategy)
    last = hist.iloc[-1]
    held = balance["positions"].get(kis_code)

    if held:
        entry_price = held["avg_price"]
        stop = stop_price(entry_price, last["atr"], CONFIG.strategy)
        if last["Low"] <= stop or bool(last["dead_cross"]):
            print(f"[매도] {symbol} {held['shares']}주")
            broker.place_order(kis_code, "sell", held["shares"])
            _record_trade(kis_code, symbol, "sell", held["shares"], last["Close"])
            return -1
        return 0

    if not bool(last["golden_cross"]):
        return 0
    if open_count >= max_positions:
        print(f"[건너뜀] {symbol}: 골든크로스지만 동시보유 한도({max_positions}종목) 도달")
        return 0

    shares = position_size(equity, last["Close"], last["atr"], CONFIG.strategy, CONFIG.risk)
    if shares > 0:
        print(f"[매수] {symbol} {shares}주 @ {last['Close']:,.0f}")
        broker.place_order(kis_code, "buy", shares)
        _record_trade(kis_code, symbol, "buy", shares, last["Close"])
        return 1
    return 0


def _process_overseas(broker, symbol: str, exchange: str, balance: dict, equity: float, open_count: int, max_positions: int) -> int:
    """반환값: 신규 진입하면 +1, 청산하면 -1, 아무 일도 없으면 0 (동시보유 종목수 갱신용)."""
    hist = compute_indicators(load_history(symbol, period="1y"), CONFIG.strategy)
    last = hist.iloc[-1]
    held = balance["positions"].get(symbol)

    if held:
        entry_price = held["avg_price"]
        stop = stop_price(entry_price, last["atr"], CONFIG.strategy)
        if last["Low"] <= stop or bool(last["dead_cross"]):
            print(f"[매도] {symbol} {held['shares']}주")
            broker.place_order_overseas(symbol, "sell", held["shares"], last["Close"], exchange)
            _record_trade(symbol, symbol, "sell", held["shares"], last["Close"])
            return -1
        return 0

    if not bool(last["golden_cross"]):
        return 0
    if open_count >= max_positions:
        print(f"[건너뜀] {symbol}: 골든크로스지만 동시보유 한도({max_positions}종목) 도달")
        return 0

    shares = position_size(equity, last["Close"], last["atr"], CONFIG.strategy, CONFIG.risk)
    if shares > 0:
        print(f"[매수] {symbol} {shares}주 @ ${last['Close']:,.2f}")
        broker.place_order_overseas(symbol, "buy", shares, last["Close"], exchange)
        _record_trade(symbol, symbol, "buy", shares, last["Close"])
        return 1
    return 0


def _record_trade(code: str, name: str, side: str, shares: int, price: float):
    """대시보드 매매일지(webapp/app.db)에도 남겨서 수동 주문과 동일하게 추적되게 한다."""
    try:
        from webapp import db

        db.add_trade(code, name, side, shares, float(price), note="auto:live")
    except Exception as e:
        print(f"  ⚠️ 매매일지 기록 실패: {e}")


def cmd_live(symbols: list | None = None, max_positions: int | None = None):
    from broker.kis import KISBroker  # 백테스트만 쓸 때는 requests/자격증명이 필요 없도록 지연 임포트
    from webapp.overseas_symbols import SYMBOLS as OVERSEAS_SYMBOLS

    symbols = symbols if symbols is not None else CONFIG.symbols
    overseas_exchange = {s["code"]: s["exchange"] for s in OVERSEAS_SYMBOLS}
    max_positions = max_positions if max_positions is not None else CONFIG.risk.max_open_positions

    broker = KISBroker(CONFIG)
    balance = broker.get_balance()
    equity = balance["cash"] + sum(
        p["shares"] * p["avg_price"] for p in balance["positions"].values()
    )

    today = str(date.today())
    state = _load_state()
    if state.get("date") != today:
        state = {"date": today, "start_of_day_equity": equity}
        _save_state(state)

    loss_ratio = (equity - state["start_of_day_equity"]) / state["start_of_day_equity"]
    if loss_ratio <= -CONFIG.risk.max_daily_loss:
        print(f"⛔ 일일 손실 한도 초과({loss_ratio:.1%}). 오늘은 신규 매매를 중단합니다.")
        return

    # 해외 잔고는 거래소별로 조회해야 해서, 실제로 해외 심볼이 있을 때만 그때그때 불러온다
    # (거래소당 한 번만 조회해서 재사용).
    overseas_balances: dict[str, dict] = {}
    open_count = len(balance["positions"])  # 국내 보유 종목수로 시작, 이후 해외/신규진입 반영

    print(f"[스캔] {len(symbols)}개 종목 확인, 동시보유 한도 {max_positions}종목 (현재 국내 {open_count}종목 보유 중)")

    for symbol in symbols:
        if symbol.endswith((".KS", ".KQ")):
            delta = _process_domestic(broker, symbol, balance, equity, open_count, max_positions)
        else:
            exchange = overseas_exchange.get(symbol, "NASD")
            if exchange not in overseas_balances:
                ob = broker.get_balance_overseas(exchange)
                ob["equity"] = ob["cash"] + sum(p["shares"] * p["avg_price"] for p in ob["positions"].values())
                overseas_balances[exchange] = ob
                open_count += len(ob["positions"])
            ob = overseas_balances[exchange]
            delta = _process_overseas(broker, symbol, exchange, ob, ob["equity"], open_count, max_positions)
        open_count += delta


def cmd_live_dry_run(
    symbols: list | None = None,
    max_positions: int | None = None,
    capital: float | None = None,
    note: str = "auto:sim",
):
    """브로커(KIS) 연결 없이, 매매일지(webapp/app.db)만으로 전략 로직을 시뮬레이션한다.
    실제/모의 주문을 전혀 넣지 않기 때문에 KIS 계좌 상태·인증과 완전히 무관하게 언제든 돌려볼 수 있다.
    포지션 사이징은 국내/해외 구분 없이 단일 명목자본(capital) 기준이라 금액 자체는 참고용이고,
    골든/데드크로스 신호가 제대로 걸리는지 + 매매일지에 잘 기록되는지 확인하는 용도다."""
    from webapp import db
    from webapp.overseas_symbols import SYMBOLS as OVERSEAS_SYMBOLS

    symbols = symbols if symbols is not None else CONFIG.symbols
    max_positions = max_positions if max_positions is not None else CONFIG.risk.max_open_positions
    equity = capital if capital is not None else CONFIG.initial_capital

    holdings = {p["code"]: p for p in db.compute_holdings()}
    open_count = len(holdings)

    print(f"[모의 스캔] {len(symbols)}개 종목, 명목자본 {equity:,.0f} 기준, "
          f"동시보유 한도 {max_positions}종목 (현재 {open_count}종목 보유 중)")

    for symbol in symbols:
        is_domestic = symbol.endswith((".KS", ".KQ"))
        code = symbol.split(".")[0] if is_domestic else symbol
        unit = "원" if is_domestic else "$"
        try:
            hist = compute_indicators(load_history(symbol, period="1y"), CONFIG.strategy)
        except Exception as e:
            print(f"[에러] {symbol}: {e}")
            continue
        last = hist.iloc[-1]
        prev = hist.iloc[-2]
        chg_pct = (last["Close"] - prev["Close"]) / prev["Close"] * 100 if prev["Close"] else 0.0
        chg_str = f"{chg_pct:+.2f}%"
        held = holdings.get(code)

        if held:
            stop = stop_price(held["avg_price"], last["atr"], CONFIG.strategy)
            if last["Low"] <= stop or bool(last["dead_cross"]):
                reason = "손절" if last["Low"] <= stop else "데드크로스"
                print(f"[모의매도] {symbol} {held['shares']}주 @ {last['Close']:,.2f}{unit} ({chg_str}) ({reason})")
                db.add_trade(code, symbol, "sell", held["shares"], float(last["Close"]), note=note)
                del holdings[code]
                open_count -= 1
            continue

        if not bool(last["golden_cross"]):
            print(f"[관찰] {symbol} {last['Close']:,.2f}{unit} ({chg_str}) - 골든크로스 아님")
            continue
        if open_count >= max_positions:
            print(f"[건너뜀] {symbol} {last['Close']:,.2f}{unit} ({chg_str}) - 골든크로스지만 동시보유 한도({max_positions}종목) 도달")
            continue

        shares = position_size(equity, last["Close"], last["atr"], CONFIG.strategy, CONFIG.risk)
        if shares > 0:
            print(f"[모의매수] {symbol} {shares}주 @ {last['Close']:,.2f}{unit} ({chg_str})")
            db.add_trade(code, symbol, "buy", shares, float(last["Close"]), note=note)
            holdings[code] = {"code": code, "name": symbol, "shares": shares, "avg_price": float(last["Close"])}
            open_count += 1
        else:
            print(f"[건너뜀] {symbol} {last['Close']:,.2f}{unit} ({chg_str}) - 골든크로스지만 계산된 매수수량이 0")


def main():
    parser = argparse.ArgumentParser(description="주식 자동매매 프로그램")
    parser.add_argument("mode", nargs="?", default="backtest", choices=["backtest", "live"])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="KIS 브로커에 실제/모의 주문을 전혀 넣지 않고, 매매일지(db)만으로 전략 로직을 시뮬레이션한다. "
             "KIS 인증/계좌 상태와 무관하게 로직 자체(골든크로스 감지, 손절, 동시보유 한도)만 테스트하고 싶을 때 사용.",
    )
    parser.add_argument(
        "--all-overseas",
        action="store_true",
        help="config.py의 symbols 외에도 webapp/overseas_symbols.py에 있는 큐레이션 해외 종목 전체를 "
             "같이 스캔한다 (로직 테스트용). 실제로 매수되는 건 여전히 동시보유 한도(max_open_positions) 안에서만.",
    )
    parser.add_argument(
        "--max-positions",
        type=int,
        default=None,
        help="이번 실행에서만 동시보유 한도(config.py의 max_open_positions)를 덮어쓴다. "
             "로직에 걸리는 종목을 최대한 다 사보면서 버그를 찾고 싶을 때 크게 잡아서 쓰면 된다 (예: 999).",
    )
    parser.add_argument(
        "--note",
        default="auto:sim",
        help="--dry-run에서 매매일지에 남길 메모(note) 값. 기본값 auto:sim.",
    )
    args = parser.parse_args()

    if args.mode == "backtest":
        cmd_backtest()
        return

    symbols = CONFIG.symbols
    if args.all_overseas:
        from webapp.overseas_symbols import SYMBOLS as OVERSEAS_SYMBOLS

        all_overseas = [s["code"] for s in OVERSEAS_SYMBOLS]
        symbols = list(dict.fromkeys(list(CONFIG.symbols) + all_overseas))  # 순서 유지 + 중복 제거
        print(f"[--all-overseas] 해외 큐레이션 종목 {len(all_overseas)}개 추가 스캔 (총 {len(symbols)}개)")

    if args.dry_run:
        cmd_live_dry_run(symbols, max_positions=args.max_positions, note=args.note)
    else:
        cmd_live(symbols, max_positions=args.max_positions)


if __name__ == "__main__":
    sys.exit(main())
