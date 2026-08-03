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


def cmd_live():
    from broker.kis import KISBroker  # 백테스트만 쓸 때는 requests/자격증명이 필요 없도록 지연 임포트

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

    for symbol in CONFIG.symbols:
        if symbol.endswith((".KS", ".KQ")):
            kis_code = symbol.split(".")[0]
        else:
            print(f"[건너뜀] {symbol}: 해외주식 주문은 아직 미구현입니다 (broker/kis.py 참고).")
            continue

        hist = compute_indicators(load_history(symbol, period="1y"), CONFIG.strategy)
        last = hist.iloc[-1]
        held = balance["positions"].get(kis_code)

        if held:
            entry_price = held["avg_price"]
            stop = stop_price(entry_price, last["atr"], CONFIG.strategy)
            if last["Low"] <= stop or bool(last["dead_cross"]):
                print(f"[매도] {symbol} {held['shares']}주")
                broker.place_order(kis_code, "sell", held["shares"])
        elif bool(last["golden_cross"]):
            shares = position_size(equity, last["Close"], last["atr"], CONFIG.strategy, CONFIG.risk)
            if shares > 0:
                print(f"[매수] {symbol} {shares}주 @ {last['Close']:,.0f}")
                broker.place_order(kis_code, "buy", shares)


def main():
    parser = argparse.ArgumentParser(description="주식 자동매매 프로그램")
    parser.add_argument("mode", nargs="?", default="backtest", choices=["backtest", "live"])
    args = parser.parse_args()

    if args.mode == "backtest":
        cmd_backtest()
    else:
        cmd_live()


if __name__ == "__main__":
    sys.exit(main())
