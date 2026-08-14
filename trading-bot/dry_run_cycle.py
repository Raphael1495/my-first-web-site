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
from data.loader import get_live_quote, load_history
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


def check_exit(yf_symbol: str, avg_price: float, mode: str, surge_params, reversion_params=None,
               entry_bar: str | None = None):
    """mode("trend"/"surge"/"reversion")에 맞는 손절/익절/데드크로스/이평선회귀 신호를
    확인해서 (exit_price, reason) 또는 걸린 게 없으면 (None, None)을 돌려준다.

    entry_bar는 매수에 쓰인 일봉의 날짜(문자열)다. 지금 조회한 최신 봉이 그거랑 같으면
    아직 실제로 새 데이터가 나오지 않은 것(장이 안 열렸거나 하루 안 지남)이므로 손절/익절
    체크를 건너뛴다 — 안 그러면 "매수에 쓴 종가"와 "같은 날의 저가"를 비교하게 되는데,
    급등주는 원래 하루 변동폭이 커서(그래서 오늘 급등한 거니까) 사자마자 같은 봉의 저가가
    손절선 아래인 경우가 흔해서 즉시 손절 → 신호 여전히 살아있으니 재매수 → 즉시 손절 하는
    무한 루프에 빠진다. 실제로 시간이 지나 새 봉이 생긴 뒤부터만 손절/익절을 판단해야 맞다."""
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

    if entry_bar is not None and str(last.name) == entry_bar:
        return None, None, None

    # 해외종목은 일봉 고저가 대신, 지금 이 순간의 실제 세션(정규장/프리마켓/애프터마켓)
    # 체결가로 손절/익절을 판단한다 — 일봉 고저가만 보면 장이 열리기 전엔 어제 데이터로
    # 계속 같은 판단을 반복하게 되고, 세션 구분 없이 뭉뚱그려지는 문제가 있었다.
    live = get_live_quote(yf_symbol) if not is_domestic(yf_symbol) else None
    current_price = live["price"] if live else None
    session = live["session"] if live else None

    if current_price is not None:
        hit_stop = current_price <= stop
        hit_target = current_price >= target
    else:
        hit_stop = last["Low"] <= stop
        hit_target = last["High"] >= target

    if not (hit_stop or hit_target or dead):
        return None, None, None
    reason = "손절" if hit_stop else (
        "익절" if hit_target else ("이평선회귀" if mode == "reversion" else "데드크로스")
    )
    if current_price is not None:
        exit_price = current_price
    else:
        exit_price = target if hit_target and not hit_stop else float(last["Close"])
    return exit_price, reason, session


def sell_phase(position_mode: dict, position_symbol: dict, surge_params, reversion_params=None,
               reason_override: str | None = None, position_entry_bar: dict | None = None):
    """position_mode: {code: "trend"|"surge"|"reversion"}, position_symbol: {code: 야후심볼} — buy_phase가 채워준다.
    reason_override가 주어지면(테스트 세션 종료 등) 신호와 무관하게 그 사유로 전량 매도한다.
    그게 아니어도 해당 시장(국내/해외) 실제 매도 시점이 지났으면 자동으로 강제청산한다."""
    from main import _is_kr_near_close, _is_us_near_close

    position_entry_bar = position_entry_bar if position_entry_bar is not None else {}
    holdings = {p["code"]: p for p in db.compute_holdings()}
    if not holdings:
        print("  (매도 대상 없음)")
        return
    kr_close = _is_kr_near_close()
    us_close = _is_us_near_close()
    for code, pos in holdings.items():
        mode = position_mode.get(code, "trend")
        yf_symbol = position_symbol.get(code, code)  # 재시작 등으로 모르면 코드 자체를 그대로 시도
        domestic = is_domestic(yf_symbol)
        near_close = kr_close if domestic else us_close
        session = None
        if reason_override or near_close:
            live = get_live_quote(yf_symbol) if not domestic else None
            if live:
                price, session = live["price"], live["session"]
            else:
                try:
                    price = float(load_history(yf_symbol, period="5d").iloc[-1]["Close"])
                except Exception:
                    price = pos["avg_price"]
            reason = reason_override or "장마감강제청산"
        else:
            try:
                price, reason, session = check_exit(yf_symbol, pos["avg_price"], mode, surge_params, reversion_params,
                                                     entry_bar=position_entry_bar.get(code))
            except Exception as e:
                print(f"  [에러] {code}({yf_symbol}) 매도 신호 확인 실패, 이번 사이클 건너뜀: {e}")
                continue
            if price is None:
                continue
        unit = "원" if domestic else "$"
        session_tag = f" [{session}]" if session else ""
        print(f"  [모의매도:{reason}]{session_tag} {code} {pos['shares']}주 @ {price:,.2f}{unit}")
        db.add_trade(code, pos["name"], "sell", pos["shares"], price, note=NOTE)
        extra = {"구분": "모의투자", "사유": reason}
        if session:
            extra["세션"] = session
        send_trade_alert(code, pos["name"], "sell", pos["shares"], price, extra=extra,
                          pnl_pct=(price - pos["avg_price"]) / pos["avg_price"] * 100)
        position_mode.pop(code, None)
        position_symbol.pop(code, None)
        position_entry_bar.pop(code, None)


def buy_phase(symbols, equity, max_positions, position_mode: dict, position_symbol: dict,
              surge_params=None, both_mode=False, reversion_params=None, position_entry_bar: dict | None = None):
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

        # 해외종목은 일봉 종가 대신, 지금 이 순간 실제 체결 가능한 세션(정규장/프리마켓/
        # 애프터마켓) 가격을 실제 매수단가로 쓴다. 신호(골든크로스/급등/이격도) 자체는
        # 여전히 일봉 지표 기준으로 판단하고, "얼마에 살 수 있는가"만 실시간 가격을 쓰는 것.
        live = get_live_quote(symbol) if not domestic else None
        fill_price = live["price"] if live else float(last["Close"])
        session = live["session"] if live else None

        if mode == "trend":
            shares = position_size(equity, fill_price, last["atr"], CONFIG.strategy, CONFIG.risk)
        elif mode == "surge":
            shares = surge_position_size(equity, fill_price, last["atr"], surge_params_for(symbol, surge_params))
        else:
            shares = reversion_position_size(equity, fill_price, last["atr"], reversion_params)
        if shares <= 0:
            print(f"  [건너뜀] {symbol} - 계산 수량 0")
            continue
        name = lookup_name(code, domestic)
        session_tag = f" [{session}]" if session else ""
        print(f"  [모의매수]{session_tag} {symbol} {shares}주 @ {fill_price:,.2f}{unit} ({mode_tag})")
        db.add_trade(code, name, "buy", shares, fill_price, note=f"{NOTE}:{mode}")
        extra = {"구분": "모의투자", "전략": mode_tag}
        if session:
            extra["세션"] = session
        send_trade_alert(code, name, "buy", shares, fill_price, extra=extra)
        db.add_watchlist(code, name, market="domestic" if domestic else "overseas")
        db.prune_watchlist({p["code"] for p in db.compute_holdings()})
        holdings[code] = {"code": code, "shares": shares}
        position_mode[code] = mode
        position_symbol[code] = symbol
        if position_entry_bar is not None:
            position_entry_bar[code] = str(last.name)
        open_count += 1
        bought.append(code)
    return bought


def _infer_position_mode(code: str) -> str:
    """재시작 시 이미 보유 중인 종목의 원래 매수 전략(trend/surge/reversion)을 매매일지
    (note가 f"{NOTE}:mode" 형식으로 기록됨)에서 찾아 복원한다. 이 방식 도입 전 기록이거나
    이 스크립트가 산 게 아니면 trend로 기본 처리한다 — CLI에 넘긴 현재 모드 플래그로
    무작정 덮어쓰면(예: --reversion으로 재시작 시 골든크로스로 산 종목까지 이평선회귀
    청산 로직이 적용되는) 엉뚱한 매도가 나갈 수 있어서 반드시 실제 기록을 봐야 한다."""
    buys = [
        t for t in db.list_trades()
        if t["code"] == code and t["side"] == "buy" and t["note"].startswith(NOTE)
    ]
    if not buys:
        return "trend"
    latest = max(buys, key=lambda t: t["traded_at"])
    for mode in ("trend", "surge", "reversion"):
        if latest["note"].endswith(f":{mode}"):
            return mode
    return "trend"


def main():
    argv = sys.argv[1:]
    surge_mode = "--surge" in argv
    both_mode = "--both" in argv
    reversion_mode = "--reversion" in argv
    argv = [a for a in argv if a not in ("--surge", "--both", "--reversion")]

    end_time = parse_end_time(argv[0] if len(argv) > 0 else "23:30")
    poll_minutes = int(argv[1]) if len(argv) > 1 else 5
    if surge_mode or both_mode:
        from data.screener import fetch_us_day_gainers

        dynamic = fetch_us_day_gainers()
        default_symbols = list(dict.fromkeys(list(CONFIG.surge_symbols_us) + dynamic))
        if dynamic:
            print(f"[dry_run_cycle] 오늘의 급등주 스크리너 {len(dynamic)}종목 추가: {dynamic}")
    elif reversion_mode:
        default_symbols = list(CONFIG.reversion_symbols)
    else:
        default_symbols = DEFAULT_SYMBOLS
    symbols = argv[2].split(",") if len(argv) > 2 and argv[2] else default_symbols
    equity = float(argv[3]) if len(argv) > 3 and argv[3] else CONFIG.initial_capital
    max_positions = int(argv[4]) if len(argv) > 4 and argv[4] else (
        CONFIG.risk.max_open_positions_surge if surge_mode else
        (CONFIG.risk.max_open_positions_reversion if reversion_mode else len(symbols))
    )
    surge_params = CONFIG.surge if (surge_mode or both_mode) else None
    reversion_params = CONFIG.reversion if reversion_mode else None

    active_strategies = [
        label for flag, label in (
            (not surge_mode and not both_mode and not reversion_mode, "골든크로스"),
            (both_mode, "골든크로스+급등주"),
            (surge_mode and not both_mode, "급등주"),
            (reversion_mode, "이평선회귀"),
        ) if flag
    ]
    mode_label = "+".join(active_strategies)
    print(f"[dry_run_cycle] {end_time.strftime('%Y-%m-%d %H:%M')}까지, {poll_minutes}분마다 재확인 ({mode_label} 전략)")
    print(f"[dry_run_cycle] 대상 {len(symbols)}종목({symbols}), 명목자본 {equity:,.2f}, 동시보유 한도 {max_positions}")
    if reversion_mode:
        print(f"[dry_run_cycle] 이평선회귀 매수 조건: 20일선 대비 -{CONFIG.reversion.entry_deviation_pct:.0f}% 이상 이격 "
              f"(⚠️ 체결강도 조건은 실시간 API 필요해서 모의투자에선 생략, 실거래에서만 적용)")
    print(f"[dry_run_cycle] 매도 조건(전략별 자동 판별): 익절 / 손절 / 데드크로스(추세추종) / "
          f"이평선회귀(이평선회귀) / 장마감청산")

    position_mode: dict = {}
    position_symbol: dict = {}
    position_entry_bar: dict = {}  # 매수에 쓴 봉 날짜 — 같은 봉으로 즉시 손절되는 걸 막는 용도
    for p in db.compute_holdings():
        code = p["code"]
        position_mode[code] = _infer_position_mode(code)
        yf_symbol = code if not code.isdigit() else f"{code}.KS"
        position_symbol[code] = yf_symbol
        # 재시작 전에 이미 보유하던 종목은 원래 매수에 쓴 봉을 알 수 없다. 그렇다고 비워두면
        # 방금 데이터 갱신 없이 재시작한 경우 "지금" 최신 봉의 저가로 즉시 손절 체크가 걸려
        # 버릴 수 있어서(=재시작이 곧 손절 트리거가 되는 문제), 지금 시점 최신 봉을 진입 봉으로
        # 간주해 최소한 그 봉 안에서는 보호되게 한다. 다음 새 봉부터 정상적으로 손절/익절이 체크된다.
        try:
            position_entry_bar[code] = str(load_history(yf_symbol, period="5d", interval="1d").iloc[-1].name)
        except Exception:
            pass

    cycle = 0
    while datetime.now() < end_time:
        cycle += 1
        print(f"\n===== 사이클 {cycle} — {datetime.now().strftime('%H:%M:%S')} =====")
        sell_phase(position_mode, position_symbol, surge_params, reversion_params,
                   position_entry_bar=position_entry_bar)
        buy_phase(symbols, equity, max_positions, position_mode, position_symbol,
                  surge_params=surge_params, both_mode=both_mode, reversion_params=reversion_params,
                  position_entry_bar=position_entry_bar)

        wait_until = min(datetime.now() + timedelta(minutes=poll_minutes), end_time)
        while datetime.now() < wait_until:
            time_mod.sleep(5)

    print(f"\n[dry_run_cycle] 종료 시각 도달. 남은 보유종목 정리.")
    sell_phase(position_mode, position_symbol, surge_params, reversion_params, reason_override="종료청산",
               position_entry_bar=position_entry_bar)
    print("[dry_run_cycle] 끝.")


if __name__ == "__main__":
    main()
