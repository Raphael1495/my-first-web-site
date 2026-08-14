"""로직 테스트 전용 스크립트 — 브로커 호출 없이, 매매일지(webapp/app.db)에만 기록한다.

동작: 지정한 종료 시각까지, POLL_MINUTES 간격으로
  1) 보유 중인 종목은 익절(+5%)/손절/데드크로스(추세추종만) 신호 확인해서 걸리면 매도
  2) 안 걸리고 자리가 남으면, 새로 골든크로스/급등 신호 뜬 종목을 매수
종료 시각이 되면 그 시점에 들고 있는 건 전부 정리하고 끝낸다.

⚠️ 예전 버전과 다르게 "N분 지나면 무조건 매도"하지 않는다 — 실제 전략처럼 신호(익절/손절)가
걸려야 매도한다. 다만 데이터가 일봉(daily bar) 기준이라, 당일 고가/저가가 목표/손절선을
스쳤는지로 판단한다 (장중 실시간 체결까지 재현하는 건 아님).

국내 종목은 "005930.KS"처럼 야후 접미사를 붙여서 넘긴다 — 매매일지엔 접미사 뗀 코드로
기록되고(대시보드 표기와 동일), 시세 조회할 땐 원래 접미사를 다시 붙여서 쓴다.

사용법: python dry_run_cycle.py HH:MM [poll_minutes(기본 5)] [symbol,symbol,...] [capital] [max_positions] [--surge|--both|--reversion]

--surge를 주면 골든크로스 대신 급등주(전일比 등락률+거래량 급증) 신호로 매수한다. 이때
symbol 목록을 안 주면 CONFIG.surge_symbols_us(해외 급등주 유니버스) 전체를 스캔한다.
--both를 주면 종목마다 골든크로스/급등주 신호를 둘 다 체크해서, 둘 중 하나라도 걸리면
매수한다 (우량주+급등주 섞어서 같이 돌릴 때 사용).
--reversion을 주면 20일 이평선 대비 -3%(entry_deviation_pct) 이상 빠졌을 때 매수, 이평선
위로 회귀하면 매도한다. ⚠️ 체결강도(매수세) 조건은 KIS 실시간 API로만 확인 가능해서 이
모의투자 스크립트는 브로커 호출 없이(로그인/토큰 없이) 돌아가야 하는 설계상 그 조건은
생략하고 이격도 조건만 본다 — 실거래(main.py reversion 모드)에서만 체결강도까지 검사한다.
"""
import sys
import time as time_mod
from dataclasses import replace
from datetime import datetime, timedelta

from config import CONFIG
from data.loader import load_history
from notify import send_trade_alert
from strategy.mean_reversion import compute_indicators as compute_reversion_signal
from strategy.mean_reversion import position_size as reversion_position_size
from strategy.mean_reversion import stop_price as reversion_stop_price
from strategy.mean_reversion import take_profit_price as reversion_take_profit_price
from strategy.momentum_surge import compute_surge_signal
from strategy.momentum_surge import position_size as surge_position_size
from strategy.momentum_surge import stop_price as surge_stop_price
from strategy.momentum_surge import take_profit_price as surge_take_profit_price
from strategy.trend_following import compute_indicators, position_size, stop_price, take_profit_price
from webapp import db
from webapp.krx_symbols import load_symbols as _load_krx_symbols
from webapp.overseas_symbols import SYMBOLS as _OVERSEAS_SYMBOLS

NOTE = "auto_test"
DEFAULT_SYMBOLS = ["AAPL", "MSFT", "AMZN", "META", "ADBE", "SBUX"]

_KRX_NAME = {s["code"]: s["name"] for s in _load_krx_symbols()}
_US_NAME = {s["code"]: s["name"] for s in _OVERSEAS_SYMBOLS}


def is_domestic(symbol: str) -> bool:
    return symbol.split(".")[0].isdigit()


def to_code(symbol: str) -> str:
    """야후 심볼("005930.KS")을 매매일지에 쓸 코드("005930")로 줄인다. 해외는 그대로."""
    return symbol.split(".")[0] if is_domestic(symbol) else symbol


def lookup_name(code: str, domestic: bool) -> str:
    """코드에 대응하는 실제 종목명을 찾는다 (못 찾으면 코드를 그대로 이름으로 씀)."""
    return (_KRX_NAME if domestic else _US_NAME).get(code, code)


def surge_params_for(symbol: str, base_params):
    """국내는 원화 거래대금 기준 그대로, 해외는 달러 기준으로 바꿔서 쓴다."""
    if is_domestic(symbol):
        return base_params
    return replace(base_params, min_trading_value=base_params.min_trading_value_usd)


def parse_end_time(hhmm: str) -> datetime:
    now = datetime.now()
    h, m = map(int, hhmm.split(":"))
    end = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return end


def check_exit(yf_symbol: str, avg_price: float, mode: str, surge_params, reversion_params=None):
    """mode("trend"/"surge"/"reversion")에 맞는 손절/익절/데드크로스/이평선회귀 신호를
    확인해서 (exit_price, reason) 또는 걸린 게 없으면 (None, None)을 돌려준다."""
    if mode == "surge":
        params = surge_params_for(yf_symbol, surge_params)
        last = compute_surge_signal(load_history(yf_symbol, period="6mo"), params).iloc[-1]
        stop = surge_stop_price(avg_price, last["atr"], params)
        target = surge_take_profit_price(avg_price, params)
        dead = False
    elif mode == "reversion":
        last = compute_reversion_signal(load_history(yf_symbol, period="1y"), reversion_params).iloc[-1]
        stop = reversion_stop_price(avg_price, last["atr"], reversion_params)
        target = reversion_take_profit_price(avg_price, reversion_params)
        dead = bool(last["reversion_exit"])
    else:
        last = compute_indicators(load_history(yf_symbol, period="1y"), CONFIG.strategy).iloc[-1]
        stop = stop_price(avg_price, last["atr"], CONFIG.strategy)
        target = take_profit_price(avg_price, CONFIG.strategy)
        dead = bool(last["dead_cross"])

    hit_stop = last["Low"] <= stop
    hit_target = last["High"] >= target
    if not (hit_stop or hit_target or dead):
        return None, None
    reason = "손절" if hit_stop else (
        "익절" if hit_target else ("이평선회귀" if mode == "reversion" else "데드크로스")
    )
    exit_price = target if hit_target and not hit_stop else float(last["Close"])
    return exit_price, reason


def sell_phase(position_mode: dict, position_symbol: dict, surge_params, reversion_params=None,
               reason_override: str | None = None):
    """position_mode: {code: "trend"|"surge"|"reversion"}, position_symbol: {code: 야후심볼} — buy_phase가 채워준다.
    reason_override가 주어지면(테스트 세션 종료 등) 신호와 무관하게 그 사유로 전량 매도한다.
    그게 아니어도 해당 시장(국내/해외) 실제 매도 시점이 지났으면 자동으로 강제청산한다."""
    from main import _is_kr_near_close, _is_us_near_close

    holdings = {p["code"]: p for p in db.compute_holdings()}
    if not holdings:
        print("  (매도 대상 없음)")
        return
    kr_close = _is_kr_near_close()
    us_close = _is_us_near_close()
    for code, pos in holdings.items():
        mode = position_mode.get(code, "trend")
        yf_symbol = position_symbol.get(code, code)  # 재시작 등으로 모르면 코드 자체를 그대로 시도
        near_close = kr_close if is_domestic(yf_symbol) else us_close
        if reason_override or near_close:
            try:
                price = float(load_history(yf_symbol, period="5d").iloc[-1]["Close"])
            except Exception:
                price = pos["avg_price"]
            reason = reason_override or "장마감강제청산"
        else:
            price, reason = check_exit(yf_symbol, pos["avg_price"], mode, surge_params, reversion_params)
            if price is None:
                continue
        unit = "원" if is_domestic(yf_symbol) else "$"
        print(f"  [모의매도:{reason}] {code} {pos['shares']}주 @ {price:,.2f}{unit}")
        db.add_trade(code, pos["name"], "sell", pos["shares"], price, note=NOTE)
        send_trade_alert(code, pos["name"], "sell", pos["shares"], price, extra={"구분": "모의투자", "사유": reason},
                          pnl_pct=(price - pos["avg_price"]) / pos["avg_price"] * 100)
        position_mode.pop(code, None)
        position_symbol.pop(code, None)


def buy_phase(symbols, equity, max_positions, position_mode: dict, position_symbol: dict,
              surge_params=None, both_mode=False, reversion_params=None):
    """surge_params가 주어지면 골든크로스 대신 급등주 신호로 매수한다.
    both_mode=True면 종목마다 골든크로스/급등주 신호를 둘 다 체크해서, 골든크로스를 우선으로
    (없으면 급등주 신호로) 산다 — 우량주와 급등주를 같은 사이클에서 섞어 돌릴 때 쓴다.
    reversion_params가 주어지면 이평선 대비 이격도 조건으로 매수한다 — ⚠️ 체결강도 조건은
    실시간 KIS API 없이는 확인 불가라 모의투자에선 생략한다(실거래에서만 추가로 검사됨)."""
    from main import _is_kr_near_close, _is_us_near_close

    kr_close = _is_kr_near_close()
    us_close = _is_us_near_close()
    holdings = {p["code"]: p for p in db.compute_holdings()}
    open_count = len(holdings)
    bought = []
    for symbol in symbols:
        code = to_code(symbol)
        domestic = is_domestic(symbol)
        unit = "원" if domestic else "$"
        if code in holdings:
            continue
        if kr_close if domestic else us_close:
            continue  # 매도 시점 지났으면 신규 진입 안 함
        try:
            trend_last = compute_indicators(load_history(symbol, period="1y"), CONFIG.strategy).iloc[-1] \
                if (both_mode or (surge_params is None and reversion_params is None)) else None
            surge_last = compute_surge_signal(load_history(symbol, period="6mo"), surge_params_for(symbol, surge_params)).iloc[-1] \
                if (both_mode or surge_params is not None) else None
            reversion_last = compute_reversion_signal(load_history(symbol, period="1y"), reversion_params).iloc[-1] \
                if reversion_params is not None else None
        except Exception as e:
            print(f"  [에러] {symbol}: {e}")
            continue

        trend_ok = trend_last is not None and bool(trend_last["golden_cross"])
        surge_ok = surge_last is not None and bool(surge_last["surge_entry"])
        reversion_ok = reversion_last is not None and bool(reversion_last["reversion_entry"])

        if trend_ok:
            last, mode, mode_tag = trend_last, "trend", "골든크로스"
        elif surge_ok:
            last, mode, mode_tag = surge_last, "surge", \
                f"급등(등락 {surge_last['change_pct']:+.1f}%, 거래량 {surge_last['volume_ratio']:.1f}배)"
        elif reversion_ok:
            last, mode, mode_tag = reversion_last, "reversion", \
                f"이평선회귀(이격도 {reversion_last['deviation_pct']:+.1f}%, ⚠️체결강도 미확인-모의투자라 생략)"
        else:
            last = trend_last if trend_last is not None else (surge_last if surge_last is not None else reversion_last)
            mode, mode_tag = None, None

        if not (trend_ok or surge_ok or reversion_ok):
            print(f"  [관찰] {symbol} {last['Close']:,.2f}{unit} - 신호 없음")
            continue
        if open_count >= max_positions:
            print(f"  [건너뜀] {symbol} - 동시보유 한도({max_positions}) 도달")
            continue

        if mode == "trend":
            shares = position_size(equity, last["Close"], last["atr"], CONFIG.strategy, CONFIG.risk)
        elif mode == "surge":
            shares = surge_position_size(equity, last["Close"], last["atr"], surge_params_for(symbol, surge_params))
        else:
            shares = reversion_position_size(equity, last["Close"], last["atr"], reversion_params)
        if shares <= 0:
            print(f"  [건너뜀] {symbol} - 계산 수량 0")
            continue
        name = lookup_name(code, domestic)
        print(f"  [모의매수] {symbol} {shares}주 @ {last['Close']:,.2f}{unit} ({mode_tag})")
        db.add_trade(code, name, "buy", shares, float(last["Close"]), note=NOTE)
        send_trade_alert(code, name, "buy", shares, float(last["Close"]), extra={"구분": "모의투자", "전략": mode_tag})
        db.add_watchlist(code, name, market="domestic" if domestic else "overseas")
        db.prune_watchlist({p["code"] for p in db.compute_holdings()})
        holdings[code] = {"code": code, "shares": shares}
        position_mode[code] = mode
        position_symbol[code] = symbol
        open_count += 1
        bought.append(code)
    return bought


def main():
    argv = sys.argv[1:]
    surge_mode = "--surge" in argv
    both_mode = "--both" in argv
    reversion_mode = "--reversion" in argv
    argv = [a for a in argv if a not in ("--surge", "--both", "--reversion")]

    end_time = parse_end_time(argv[0] if len(argv) > 0 else "23:30")
    poll_minutes = int(argv[1]) if len(argv) > 1 else 5
    if reversion_mode:
        default_symbols = list(CONFIG.reversion_symbols)
    elif surge_mode or both_mode:
        default_symbols = list(CONFIG.surge_symbols_us)
    else:
        default_symbols = DEFAULT_SYMBOLS
    symbols = argv[2].split(",") if len(argv) > 2 and argv[2] else default_symbols
    equity = float(argv[3]) if len(argv) > 3 and argv[3] else CONFIG.initial_capital
    max_positions = int(argv[4]) if len(argv) > 4 and argv[4] else (
        CONFIG.risk.max_open_positions_reversion if reversion_mode else
        (CONFIG.risk.max_open_positions_surge if surge_mode else len(symbols))
    )
    surge_params = CONFIG.surge if (surge_mode or both_mode) else None
    reversion_params = CONFIG.reversion if reversion_mode else None

    mode_label = "이평선회귀" if reversion_mode else (
        "우량주+급등주 혼합" if both_mode else ("급등주" if surge_mode else "골든크로스")
    )
    print(f"[dry_run_cycle] {end_time.strftime('%Y-%m-%d %H:%M')}까지, {poll_minutes}분마다 재확인 ({mode_label} 전략)")
    print(f"[dry_run_cycle] 대상 {len(symbols)}종목({symbols}), 명목자본 {equity:,.2f}, 동시보유 한도 {max_positions}")
    if reversion_mode:
        print(f"[dry_run_cycle] 매수 조건: 20일선 대비 -{CONFIG.reversion.entry_deviation_pct:.0f}% 이상 이격 "
              f"(⚠️ 체결강도 조건은 실시간 API 필요해서 모의투자에선 생략, 실거래에서만 적용)")
        print(f"[dry_run_cycle] 매도 조건: 이평선회귀 / 손절 / 익절(+{CONFIG.reversion.take_profit_pct:.0%}) / 장마감청산")
    else:
        print(f"[dry_run_cycle] 매도 조건: 익절(+{CONFIG.strategy.take_profit_pct:.0%}) / 손절 / 데드크로스(추세추종만) / 장마감청산")

    position_mode: dict = {}
    position_symbol: dict = {}
    for p in db.compute_holdings():
        position_mode[p["code"]] = "reversion" if reversion_mode else "trend"
        position_symbol[p["code"]] = p["code"] if not p["code"].isdigit() else f"{p['code']}.KS"

    cycle = 0
    while datetime.now() < end_time:
        cycle += 1
        print(f"\n===== 사이클 {cycle} — {datetime.now().strftime('%H:%M:%S')} =====")
        sell_phase(position_mode, position_symbol, surge_params, reversion_params)
        buy_phase(symbols, equity, max_positions, position_mode, position_symbol,
                  surge_params=surge_params, both_mode=both_mode, reversion_params=reversion_params)

        wait_until = min(datetime.now() + timedelta(minutes=poll_minutes), end_time)
        while datetime.now() < wait_until:
            time_mod.sleep(5)

    print(f"\n[dry_run_cycle] 종료 시각 도달. 남은 보유종목 정리.")
    sell_phase(position_mode, position_symbol, surge_params, reversion_params, reason_override="종료청산")
    print("[dry_run_cycle] 끝.")


if __name__ == "__main__":
    main()
