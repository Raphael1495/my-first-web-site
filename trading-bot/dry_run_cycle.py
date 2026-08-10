"""로직 테스트 전용 스크립트 — 브로커 호출 없이, 매매일지(webapp/app.db)에만 기록한다.

동작: 지정한 종료 시각까지, 몇 분 간격으로
  1) 6개 종목 중 골든크로스 걸린 것 중 아직 안 산 것 매수(시뮬레이션)
  2) HOLD_MINUTES 분 대기
  3) 그 사이 산 것들 강제 매도(시뮬레이션) — 실제 전략의 데드크로스/손절 신호를 기다리는 게
     아니라, 매수→매도 사이클 자체가 매매일지에 잘 기록되는지 빠르게 반복 테스트하기 위한
     인위적인 시간 청산이다. (실제 전략의 청산 로직과는 다름, 로직 구멍 찾기용)
종료 시각이 되면 그 시점에 들고 있는 건 전부 정리하고 끝낸다.

사용법: python dry_run_cycle.py HH:MM [hold_minutes(기본 5)] [symbol,symbol,...]
"""
import sys
import time as time_mod
from datetime import datetime, timedelta

from config import CONFIG
from data.loader import load_history
from strategy.trend_following import compute_indicators, position_size, stop_price
from webapp import db

NOTE = "auto_test"
DEFAULT_SYMBOLS = ["AAPL", "MSFT", "AMZN", "META", "ADBE", "SBUX"]


def parse_end_time(hhmm: str) -> datetime:
    now = datetime.now()
    h, m = map(int, hhmm.split(":"))
    end = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return end


def quote(symbol: str):
    hist = compute_indicators(load_history(symbol, period="1y"), CONFIG.strategy)
    last = hist.iloc[-1]
    prev = hist.iloc[-2]
    pct = (last["Close"] - prev["Close"]) / prev["Close"] * 100 if prev["Close"] else 0.0
    return last, pct


def buy_phase(symbols, equity, max_positions):
    holdings = {p["code"]: p for p in db.compute_holdings()}
    open_count = len(holdings)
    bought = []
    for symbol in symbols:
        if symbol in holdings:
            continue
        try:
            last, pct = quote(symbol)
        except Exception as e:
            print(f"  [에러] {symbol}: {e}")
            continue
        if not bool(last["golden_cross"]):
            print(f"  [관찰] {symbol} {last['Close']:.2f}$ ({pct:+.2f}%) - 골든크로스 아님")
            continue
        if open_count >= max_positions:
            print(f"  [건너뜀] {symbol} - 동시보유 한도({max_positions}) 도달")
            continue
        shares = position_size(equity, last["Close"], last["atr"], CONFIG.strategy, CONFIG.risk)
        if shares <= 0:
            print(f"  [건너뜀] {symbol} - 계산 수량 0")
            continue
        print(f"  [모의매수] {symbol} {shares}주 @ {last['Close']:.2f}$ ({pct:+.2f}%)")
        db.add_trade(symbol, symbol, "buy", shares, float(last["Close"]), note=NOTE)
        holdings[symbol] = {"code": symbol, "shares": shares}
        open_count += 1
        bought.append(symbol)
    return bought


def sell_all(reason: str):
    holdings = db.compute_holdings()
    if not holdings:
        print(f"  (청산할 보유종목 없음)")
        return
    for pos in holdings:
        try:
            last, pct = quote(pos["code"])
            price = float(last["Close"])
        except Exception:
            price = pos["avg_price"]
        print(f"  [모의매도:{reason}] {pos['code']} {pos['shares']}주 @ {price:.2f}$")
        db.add_trade(pos["code"], pos["name"], "sell", pos["shares"], price, note=NOTE)


def main():
    end_time = parse_end_time(sys.argv[1] if len(sys.argv) > 1 else "23:30")
    hold_minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    symbols = sys.argv[3].split(",") if len(sys.argv) > 3 else DEFAULT_SYMBOLS
    max_positions = len(symbols)
    equity = CONFIG.initial_capital

    print(f"[dry_run_cycle] {end_time.strftime('%H:%M')}까지, {hold_minutes}분 보유 후 강제매도 반복")
    print(f"[dry_run_cycle] 대상: {symbols}")

    cycle = 0
    while datetime.now() < end_time:
        cycle += 1
        print(f"\n===== 사이클 {cycle} — {datetime.now().strftime('%H:%M:%S')} =====")
        bought = buy_phase(symbols, equity, max_positions)

        wait_until = min(datetime.now() + timedelta(minutes=hold_minutes), end_time)
        if bought:
            print(f"  -> {hold_minutes}분 대기 후 매도 ({wait_until.strftime('%H:%M:%S')}까지)")
        while datetime.now() < wait_until:
            time_mod.sleep(5)

        if datetime.now() >= end_time:
            break
        print(f"----- {datetime.now().strftime('%H:%M:%S')} 매도 페이즈 -----")
        sell_all("보유시간종료")

    print(f"\n[dry_run_cycle] 종료 시각 도달. 남은 보유종목 정리.")
    sell_all("종료청산")
    print("[dry_run_cycle] 끝.")


if __name__ == "__main__":
    main()
