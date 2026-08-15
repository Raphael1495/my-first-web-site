"""당일 초단기 스캘핑 모의투자 스크립트 — 브로커 호출 없이, 매매일지(webapp/app.db)에만 기록한다.

동작:
  1) 시작 시점에 급등주 후보(스크리너+큐레이션)를 오늘자 등락률로 정렬해서 상위 N개(기본 5개)만 뽑는다.
  2) 그 N개 종목의 1분봉을 주기적으로 확인하면서, "급락 → 바닥다짐 → 매수세 유입" 패턴이 나오면 매수.
  3) 보유 중인 종목은 매 사이클 익절/손절/모멘텀정체 신호를 확인해서 걸리면 매도.
  4) 종료 시각(기본 미국장 마감 04:00 KST) 되면 남은 포지션 전부 정리하고 끝낸다.

일봉 기반 급등주 전략(dry_run_cycle.py --surge)과 달리 이 스크립트는 진입/청산 판단을
전부 1분봉으로 하기 때문에, 미국 정규장이 실제로 열려 있을 때 돌려야 의미가 있다.

사용법: python intraday_scalp.py [HH:MM(종료시각, 기본 04:00)] [top_n(기본 5)] [capital(기본 CONFIG.initial_capital)]
"""
import sys
import time as time_mod
from datetime import datetime, timedelta

from config import CONFIG
from data.loader import get_live_quote, load_history
from notify import send_trade_alert
from strategy.intraday_momentum import IntradayParams, check_exit_signal, find_entry_signal, position_size
from webapp import db
from webapp.overseas_symbols import SYMBOLS as _OVERSEAS_SYMBOLS

NOTE = "auto_test:intraday"
PARAMS = IntradayParams()

_US_NAME = {s["code"]: s["name"] for s in _OVERSEAS_SYMBOLS}


def lookup_name(code: str) -> str:
    return _US_NAME.get(code, code)


def parse_end_time(hhmm: str) -> datetime:
    now = datetime.now()
    h, m = map(int, hhmm.split(":"))
    end = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return end


def pick_top_candidates(top_n: int) -> list[str]:
    """급등주 후보군(스크리너+큐레이션)을 오늘 실시간 등락률로 정렬해서 상위 top_n개를 고른다."""
    from data.screener import fetch_us_surge_candidates

    universe = list(dict.fromkeys(list(CONFIG.surge_symbols_us) + fetch_us_surge_candidates()))
    ranked = []
    for symbol in universe:
        try:
            q = get_live_quote(symbol)
            if not q or not q.get("prev_close"):
                continue
            pct = (q["price"] - q["prev_close"]) / q["prev_close"] * 100
            ranked.append((symbol, pct))
        except Exception:
            continue
    ranked.sort(key=lambda x: x[1], reverse=True)
    top = ranked[:top_n]
    print(f"[intraday_scalp] 후보 {len(universe)}종목 중 오늘 등락률 상위 {len(top)}개: "
          f"{[(s, round(p, 1)) for s, p in top]}")
    return [s for s, _ in top]


def fetch_minute_bars(symbol: str):
    df = load_history(symbol, period="1d", interval="1m")
    if len(df) == 0:
        raise ValueError(f"{symbol}: 분봉 데이터 없음")
    return df


def sell_phase(positions: dict, reason_override: str | None = None):
    """positions: {code: {"entry_price": float, "entry_index": Timestamp, "shares": int, "name": str}}"""
    from main import _is_us_near_close

    if not positions:
        print("  (매도 대상 없음)")
        return
    us_close = _is_us_near_close()
    for code in list(positions.keys()):
        pos = positions[code]
        try:
            df = fetch_minute_bars(code)
        except Exception as e:
            print(f"  [에러] {code} 분봉 조회 실패, 이번 사이클 건너뜀: {e}")
            continue

        if reason_override or us_close:
            price = float(df.iloc[-1]["Close"])
            reason = reason_override or "장마감강제청산"
        else:
            result = check_exit_signal(df, pos["entry_price"], pos["entry_index"], PARAMS)
            if result is None:
                continue
            _, reason = result
            price = float(df.iloc[-1]["Close"])

        pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        print(f"  [모의매도:{reason}] {code} {pos['shares']}주 @ ${price:,.2f} ({pnl_pct:+.1f}%)")
        db.add_trade(code, pos["name"], "sell", pos["shares"], price, note=NOTE)
        send_trade_alert(code, pos["name"], "sell", pos["shares"], price,
                          extra={"구분": "모의투자(분봉)", "사유": reason}, pnl_pct=pnl_pct)
        del positions[code]


def buy_phase(candidates: list[str], equity: float, max_positions: int, positions: dict):
    from main import _is_us_near_close

    if _is_us_near_close():
        return
    if len(positions) >= max_positions:
        return
    for symbol in candidates:
        if symbol in positions:
            continue
        if len(positions) >= max_positions:
            break
        try:
            df = fetch_minute_bars(symbol)
        except Exception as e:
            print(f"  [에러] {symbol} 분봉 조회 실패: {e}")
            continue

        signal = find_entry_signal(df, PARAMS)
        if signal is None:
            continue

        shares = position_size(equity, signal["entry_price"], PARAMS)
        if shares <= 0:
            print(f"  [건너뜀] {symbol} - 계산 수량 0")
            continue

        name = lookup_name(symbol)
        entry_index = df.index[-1]
        print(f"  [모의매수] {symbol} {shares}주 @ ${signal['entry_price']:,.2f} "
              f"(고점대비 {signal['drop_pct']:.1f}% 급락 후 바닥다짐, 거래량 {signal['volume_mult']:.1f}배 유입)")
        db.add_trade(symbol, name, "buy", shares, signal["entry_price"], note=NOTE)
        send_trade_alert(symbol, name, "buy", shares, signal["entry_price"], extra={
            "구분": "모의투자(분봉)",
            "고점대비 급락": f"{signal['drop_pct']:.1f}%",
            "거래량": f"{signal['volume_mult']:.1f}배",
        })
        db.add_watchlist(symbol, name, market="overseas")
        positions[symbol] = {
            "entry_price": signal["entry_price"], "entry_index": entry_index,
            "shares": shares, "name": name,
        }


def main():
    argv = sys.argv[1:]
    end_time = parse_end_time(argv[0] if len(argv) > 0 else "04:00")
    top_n = int(argv[1]) if len(argv) > 1 else 5
    equity = float(argv[2]) if len(argv) > 2 else CONFIG.initial_capital
    poll_seconds = 60

    print(f"[intraday_scalp] {end_time.strftime('%Y-%m-%d %H:%M')}까지, {poll_seconds}초마다 재확인 (분봉 스캘핑)")
    print(f"[intraday_scalp] 명목자본 {equity:,.2f}, 동시보유 한도 {top_n}종목")
    print(f"[intraday_scalp] 진입: 최근고점대비 -{PARAMS.drop_pct}% 급락 후 {PARAMS.base_minutes}분 다짐"
          f"(고저폭 {PARAMS.base_range_pct}% 이내) + 거래량 {PARAMS.breakout_volume_mult}배 유입 시 매수")
    print(f"[intraday_scalp] 청산: 익절(+{PARAMS.take_profit_pct}%) / 손절(-{PARAMS.stop_loss_pct}%) / "
          f"모멘텀정체({PARAMS.stall_minutes}분 신고가 갱신 없음) / 장마감강제청산")

    candidates = pick_top_candidates(top_n)

    positions: dict = {}
    for p in db.compute_holdings():
        if p["code"] in candidates or not p["code"].isdigit():
            try:
                df = fetch_minute_bars(p["code"])
                positions[p["code"]] = {
                    "entry_price": p["avg_price"], "entry_index": df.index[-1],
                    "shares": p["shares"], "name": p["name"],
                }
            except Exception:
                pass

    cycle = 0
    while datetime.now() < end_time:
        cycle += 1
        print(f"\n===== 사이클 {cycle} — {datetime.now().strftime('%H:%M:%S')} =====")
        sell_phase(positions)
        buy_phase(candidates, equity, top_n, positions)

        wait_until = min(datetime.now() + timedelta(seconds=poll_seconds), end_time)
        while datetime.now() < wait_until:
            time_mod.sleep(5)

    print(f"\n[intraday_scalp] 종료 시각 도달. 남은 보유종목 정리.")
    sell_phase(positions, reason_override="종료청산")
    print("[intraday_scalp] 끝.")


if __name__ == "__main__":
    main()
