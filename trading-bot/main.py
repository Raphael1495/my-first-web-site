import argparse
import json
import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest.engine import run_backtest
from config import CONFIG, EOD_FLATTEN_KR_HOUR, EOD_FLATTEN_KR_MINUTE, EOD_FLATTEN_US_HOUR, EOD_FLATTEN_US_MINUTE
from data.loader import get_live_quote, load_history
from strategy.trend_following import compute_indicators, position_size, stop_price, take_profit_price

STATE_FILE = Path(__file__).parent / ".state.json"


def _is_kr_near_close() -> bool:
    """국내(KRX) 전량 매도 시점 지났는지 — 한국시간(Asia/Seoul) 기준
    EOD_FLATTEN_KR_HOUR:MINUTE 이후면 True. 국내장은 하루 안에 끝나는 세션이라
    "그 시각 이후" 그대로 비교해도 된다."""
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    flatten_at = now.replace(hour=EOD_FLATTEN_KR_HOUR, minute=EOD_FLATTEN_KR_MINUTE, second=0, microsecond=0)
    return now >= flatten_at


def _is_us_near_close() -> bool:
    """해외(미국) 전량 매도 시점 지났는지 — 한국시간 기준 EOD_FLATTEN_US_HOUR:MINUTE 이후면 True.
    미국장은 밤 10시반에 열려서 자정을 넘겨 다음날 새벽에 끝나는 세션이라, 단순히
    "그 시각 이후"로만 비교하면 개장 직후(예: 밤 11시)에도 "마감 임박"으로 잘못 걸린다.
    그래서 정확히 EOD_FLATTEN_US_HOUR 시간대(예: 04시대) 안에 있을 때만 True로 좁혀서 본다."""
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    return now.hour == EOD_FLATTEN_US_HOUR and now.minute >= EOD_FLATTEN_US_MINUTE


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


def _process_domestic(broker, symbol: str, balance: dict, equity: float, open_count: int, max_positions: int,
                       near_close: bool = False) -> int:
    """반환값: 신규 진입하면 +1, 청산하면 -1, 아무 일도 없으면 0 (동시보유 종목수 갱신용).
    near_close=True면 신규 진입은 멈추고, 보유분은 신호와 무관하게 당일 강제청산한다."""
    kis_code = symbol.split(".")[0]
    hist = compute_indicators(load_history(symbol, period="1y"), CONFIG.strategy)
    last = hist.iloc[-1]
    held = balance["positions"].get(kis_code)

    if held:
        entry_price = held["avg_price"]
        stop = stop_price(entry_price, last["atr"], CONFIG.strategy)
        target = take_profit_price(entry_price, CONFIG.strategy)
        hit_stop = last["Low"] <= stop
        hit_target = last["High"] >= target
        signal_exit = hit_stop or hit_target or bool(last["dead_cross"])
        if signal_exit or near_close:
            reason = "장마감강제청산" if near_close and not signal_exit else (
                "손절" if hit_stop else ("익절" if hit_target else "데드크로스")
            )
            exit_price = target if hit_target and not hit_stop else last["Close"]
            print(f"[매도] {symbol} {held['shares']}주 @ {exit_price:,.0f} ({reason})")
            broker.place_order(kis_code, "sell", held["shares"])
            _record_trade(kis_code, symbol, "sell", held["shares"], exit_price,
                           note=f"auto:live {reason}", extra={"사유": reason},
                           pnl_pct=(exit_price - entry_price) / entry_price * 100)
            return -1
        return 0

    if near_close:
        return 0  # 마감 임박이면 신규 진입 안 함
    if not bool(last["golden_cross"]):
        return 0
    if open_count >= max_positions:
        print(f"[건너뜀] {symbol}: 골든크로스지만 동시보유 한도({max_positions}종목) 도달")
        return 0

    shares = position_size(equity, last["Close"], last["atr"], CONFIG.strategy, CONFIG.risk)
    if shares > 0:
        print(f"[매수] {symbol} {shares}주 @ {last['Close']:,.0f}")
        broker.place_order(kis_code, "buy", shares)
        _record_trade(kis_code, symbol, "buy", shares, last["Close"], extra={"전략": "골든크로스"})
        return 1
    return 0


def _process_overseas(broker, symbol: str, exchange: str, balance: dict, equity: float, open_count: int,
                       max_positions: int, near_close: bool = False) -> int:
    """반환값: 신규 진입하면 +1, 청산하면 -1, 아무 일도 없으면 0 (동시보유 종목수 갱신용).
    near_close=True면 신규 진입은 멈추고, 보유분은 신호와 무관하게 당일 강제청산한다."""
    hist = compute_indicators(load_history(symbol, period="1y"), CONFIG.strategy)
    last = hist.iloc[-1]
    held = balance["positions"].get(symbol)

    live = get_live_quote(symbol)  # 정규장/프리마켓/애프터마켓 중 가장 최근 실제 체결가
    current_price = live["price"] if live else None
    session = live["session"] if live else None

    if held:
        entry_price = held["avg_price"]
        stop = stop_price(entry_price, last["atr"], CONFIG.strategy)
        target = take_profit_price(entry_price, CONFIG.strategy)
        if current_price is not None:
            hit_stop = current_price <= stop
            hit_target = current_price >= target
        else:
            hit_stop = last["Low"] <= stop
            hit_target = last["High"] >= target
        signal_exit = hit_stop or hit_target or bool(last["dead_cross"])
        if signal_exit or near_close:
            reason = "장마감강제청산" if near_close and not signal_exit else (
                "손절" if hit_stop else ("익절" if hit_target else "데드크로스")
            )
            exit_price = current_price if current_price is not None else (
                target if hit_target and not hit_stop else last["Close"])
            session_tag = f" [{session}]" if session else ""
            print(f"[매도]{session_tag} {symbol} {held['shares']}주 @ ${exit_price:,.2f} ({reason})")
            broker.place_order_overseas(symbol, "sell", held["shares"], exit_price, exchange)
            extra = {"사유": reason}
            if session:
                extra["세션"] = session
            _record_trade(symbol, symbol, "sell", held["shares"], exit_price,
                           note=f"auto:live {reason}", extra=extra,
                           pnl_pct=(exit_price - entry_price) / entry_price * 100)
            return -1
        return 0

    if near_close:
        return 0
    if not bool(last["golden_cross"]):
        return 0
    if open_count >= max_positions:
        print(f"[건너뜀] {symbol}: 골든크로스지만 동시보유 한도({max_positions}종목) 도달")
        return 0

    fill_price = current_price if current_price is not None else last["Close"]
    shares = position_size(equity, fill_price, last["atr"], CONFIG.strategy, CONFIG.risk)
    if shares > 0:
        session_tag = f" [{session}]" if session else ""
        print(f"[매수]{session_tag} {symbol} {shares}주 @ ${fill_price:,.2f}")
        broker.place_order_overseas(symbol, "buy", shares, fill_price, exchange)
        extra = {"전략": "골든크로스"}
        if session:
            extra["세션"] = session
        _record_trade(symbol, symbol, "buy", shares, fill_price, extra=extra)
        return 1
    return 0


def _record_trade(code: str, name: str, side: str, shares: int, price: float, note: str = "auto:live",
                   extra: dict | None = None, pnl_pct: float | None = None):
    """대시보드 매매일지(webapp/app.db)에도 남겨서 수동 주문과 동일하게 추적되게 한다.
    매수인 경우엔 관심종목에도 같이 넣어서 대시보드 상단에서 바로 보이게 한다.
    extra는 텔레그램 알림에 종목명/코드/수량/단가 아래로 덧붙일 상세 정보(사유, 체결강도 등).
    pnl_pct는 매도일 때 매수평단가 대비 손익률(%) — 알림에 "수익률" 줄로 붙는다."""
    try:
        from webapp import db

        db.add_trade(code, name, side, shares, float(price), note=note)
        if side == "buy":
            market = "domestic" if code.isdigit() else "overseas"
            db.add_watchlist(code, name, market=market)
            db.prune_watchlist({p["code"] for p in db.compute_holdings()})
    except Exception as e:
        print(f"  ⚠️ 매매일지 기록 실패: {e}")

    from notify import send_trade_alert

    send_trade_alert(code, name, side, shares, float(price), extra=extra, pnl_pct=pnl_pct)


def _todays_strategy_buy_codes(note_prefix: str = "auto:surge", market: str = "all") -> set:
    """오늘 해당 전략(note_prefix)으로 매수한 종목 코드 집합. 장중엔 이 전략이 실제로 들고 있는
    포지션이 뭔지 판별하는 용도로, 마감 직전엔 강제청산 대상을 고르는 용도로 쓴다.
    market: "domestic"(숫자 코드) / "overseas"(티커) / "all" 로 필터링."""
    from webapp import db

    today = str(date.today())
    codes = {
        t["code"] for t in db.list_trades()
        if t["side"] == "buy" and t["note"].startswith(note_prefix) and t["traded_at"].startswith(today)
    }
    if market == "domestic":
        return {c for c in codes if c.isdigit()}
    if market == "overseas":
        return {c for c in codes if not c.isdigit()}
    return codes


def _flatten_strategy_positions(broker, balance: dict, note_prefix: str = "auto:surge", label: str = "급등주-국내"):
    """당일 해당 전략(국내)으로 산 포지션을 전량 시장가로 청산한다 (마감 30분 전용)."""
    codes = _todays_strategy_buy_codes(note_prefix, "domestic")
    if not codes:
        print(f"[{label} 강제청산] 오늘 매수한 포지션 없음")
        return
    for code in codes:
        held = balance["positions"].get(code)
        if not held or held["shares"] <= 0:
            continue
        try:
            price = broker.get_price(code)
        except Exception:
            price = held["avg_price"]
        print(f"[{label} 강제청산] {code} {held['shares']}주 @ {price:,.0f}")
        broker.place_order(code, "sell", held["shares"])
        _record_trade(code, code, "sell", held["shares"], price, note=f"{note_prefix} 장마감강제청산",
                      extra={"사유": "장마감강제청산"}, pnl_pct=(price - held["avg_price"]) / held["avg_price"] * 100)


def _flatten_strategy_positions_overseas(broker, get_ob, overseas_exchange: dict,
                                          note_prefix: str = "auto:surge", label: str = "급등주-해외"):
    """당일 해당 전략(해외)으로 산 포지션을 전량 청산한다 (마감 30분 전용).
    get_ob(exchange)는 거래소별 해외 잔고를 캐시해서 돌려주는 콜백."""
    codes = _todays_strategy_buy_codes(note_prefix, "overseas")
    if not codes:
        print(f"[{label} 강제청산] 오늘 매수한 포지션 없음")
        return
    for code in codes:
        exchange = overseas_exchange.get(code, "NASD")
        ob = get_ob(exchange)
        held = ob["positions"].get(code)
        if not held or held["shares"] <= 0:
            continue
        try:
            price = float(load_history(code, period="5d").iloc[-1]["Close"])
        except Exception:
            price = held["avg_price"]
        print(f"[{label} 강제청산] {code} {held['shares']}주 @ ${price:,.2f}")
        broker.place_order_overseas(code, "sell", held["shares"], price, exchange)
        _record_trade(code, code, "sell", held["shares"], price, note=f"{note_prefix} 장마감강제청산",
                      extra={"사유": "장마감강제청산"}, pnl_pct=(price - held["avg_price"]) / held["avg_price"] * 100)


def _process_surge(broker, symbol: str, balance: dict, equity: float, open_count: int,
                    max_positions: int, params, surge_held_codes: set) -> int:
    """국내 급등주 처리. 반환값: 신규 진입하면 +1, 청산하면 -1, 아무 일도 없으면 0."""
    from strategy.momentum_surge import compute_surge_signal
    from strategy.momentum_surge import position_size as surge_position_size
    from strategy.momentum_surge import stop_price as surge_stop_price
    from strategy.momentum_surge import take_profit_price as surge_take_profit_price

    code = symbol.split(".")[0]
    hist = compute_surge_signal(load_history(symbol, period="6mo"), params)
    last = hist.iloc[-1]

    if code in surge_held_codes:
        held = balance["positions"].get(code)
        if not held:
            return 0
        stop = surge_stop_price(held["avg_price"], last["atr"], params)
        target = surge_take_profit_price(held["avg_price"], params)
        hit_stop = last["Low"] <= stop
        hit_target = last["High"] >= target
        if hit_stop or hit_target:
            reason = "손절" if hit_stop else "익절"
            exit_price = target if hit_target and not hit_stop else last["Close"]
            print(f"[급등주 매도] {symbol} {held['shares']}주 @ {exit_price:,.0f} ({reason})")
            broker.place_order(code, "sell", held["shares"])
            _record_trade(code, symbol, "sell", held["shares"], exit_price, note=f"auto:surge {reason}",
                          extra={"사유": reason},
                          pnl_pct=(exit_price - held["avg_price"]) / held["avg_price"] * 100)
            return -1
        return 0

    # 이미 다른 전략(추세추종 등)이 들고 있는 종목이면 겹치지 않게 건너뛴다.
    if balance["positions"].get(code):
        return 0

    if not bool(last["surge_entry"]):
        return 0
    if open_count >= max_positions:
        print(f"[건너뜀] {symbol}: 급등 신호지만 동시보유 한도({max_positions}종목) 도달")
        return 0

    shares = surge_position_size(equity, last["Close"], last["atr"], params)
    if shares > 0:
        print(f"[급등주 매수] {symbol} {shares}주 @ {last['Close']:,.0f} "
              f"(등락률 {last['change_pct']:+.1f}%, 거래량 {last['volume_ratio']:.1f}배)")
        broker.place_order(code, "buy", shares)
        _record_trade(code, symbol, "buy", shares, last["Close"], note="auto:surge",
                      extra={"등락률": f"{last['change_pct']:+.1f}%", "거래량": f"{last['volume_ratio']:.1f}배"})
        return 1
    return 0


def _process_surge_overseas(broker, symbol: str, exchange: str, balance: dict, equity: float, open_count: int,
                             max_positions: int, params, surge_held_codes: set) -> int:
    """해외 급등주 처리 (symbol은 이미 순수 티커, 예: AAPL). 반환값: +1/-1/0 (국내판과 동일)."""
    from strategy.momentum_surge import compute_surge_signal
    from strategy.momentum_surge import position_size as surge_position_size
    from strategy.momentum_surge import stop_price as surge_stop_price
    from strategy.momentum_surge import take_profit_price as surge_take_profit_price

    # 거래대금 최소 기준만 달러 단위로 바꿔서 쓴다 (국내 원화 기준 그대로 쓰면 거의 다 걸러짐).
    params_usd = replace(params, min_trading_value=params.min_trading_value_usd)
    hist = compute_surge_signal(load_history(symbol, period="6mo"), params_usd)
    last = hist.iloc[-1]

    live = get_live_quote(symbol)
    current_price = live["price"] if live else None
    session = live["session"] if live else None

    if symbol in surge_held_codes:
        held = balance["positions"].get(symbol)
        if not held:
            return 0
        stop = surge_stop_price(held["avg_price"], last["atr"], params_usd)
        target = surge_take_profit_price(held["avg_price"], params_usd)
        if current_price is not None:
            hit_stop = current_price <= stop
            hit_target = current_price >= target
        else:
            hit_stop = last["Low"] <= stop
            hit_target = last["High"] >= target
        if hit_stop or hit_target:
            reason = "손절" if hit_stop else "익절"
            exit_price = current_price if current_price is not None else (
                target if hit_target and not hit_stop else last["Close"])
            session_tag = f" [{session}]" if session else ""
            print(f"[급등주-해외 매도]{session_tag} {symbol} {held['shares']}주 @ ${exit_price:,.2f} ({reason})")
            broker.place_order_overseas(symbol, "sell", held["shares"], exit_price, exchange)
            extra = {"사유": reason}
            if session:
                extra["세션"] = session
            _record_trade(symbol, symbol, "sell", held["shares"], exit_price, note=f"auto:surge {reason}",
                          extra=extra,
                          pnl_pct=(exit_price - held["avg_price"]) / held["avg_price"] * 100)
            return -1
        return 0

    if balance["positions"].get(symbol):
        return 0

    if not bool(last["surge_entry"]):
        return 0
    if open_count >= max_positions:
        print(f"[건너뜀] {symbol}: 급등 신호지만 동시보유 한도({max_positions}종목) 도달")
        return 0

    fill_price = current_price if current_price is not None else last["Close"]
    shares = surge_position_size(equity, fill_price, last["atr"], params_usd)
    if shares > 0:
        session_tag = f" [{session}]" if session else ""
        print(f"[급등주-해외 매수]{session_tag} {symbol} {shares}주 @ ${fill_price:,.2f} "
              f"(등락률 {last['change_pct']:+.1f}%, 거래량 {last['volume_ratio']:.1f}배)")
        broker.place_order_overseas(symbol, "buy", shares, fill_price, exchange)
        extra = {"등락률": f"{last['change_pct']:+.1f}%", "거래량": f"{last['volume_ratio']:.1f}배"}
        if session:
            extra["세션"] = session
        _record_trade(symbol, symbol, "buy", shares, fill_price, note="auto:surge", extra=extra)
        return 1
    return 0


def _process_reversion(broker, symbol: str, balance: dict, equity: float, open_count: int,
                        max_positions: int, params, reversion_held_codes: set) -> int:
    """국내 이평선회귀 처리. 20일선 대비 -3%(entry_deviation_pct) 이상 빠지면 매수, 이평선
    위로 다시 회귀하면 매도. 체결강도(매수세) 조건은 실시간 API로만 확인 가능해서 실거래인
    여기서만 추가로 검사한다 (백테스트/모의투자는 이격도 조건만 본다)."""
    from strategy.mean_reversion import compute_indicators as compute_reversion_signal
    from strategy.mean_reversion import position_size as reversion_position_size
    from strategy.mean_reversion import stop_price as reversion_stop_price
    from strategy.mean_reversion import take_profit_price as reversion_take_profit_price

    code = symbol.split(".")[0]
    hist = compute_reversion_signal(load_history(symbol, period="1y"), params)
    last = hist.iloc[-1]

    if code in reversion_held_codes:
        held = balance["positions"].get(code)
        if not held:
            return 0
        stop = reversion_stop_price(held["avg_price"], last["atr"], params)
        target = reversion_take_profit_price(held["avg_price"], params)
        hit_stop = last["Low"] <= stop
        hit_target = last["High"] >= target
        reverted = bool(last["reversion_exit"])
        if hit_stop or hit_target or reverted:
            reason = "손절" if hit_stop else ("익절" if hit_target else "이평선회귀")
            exit_price = target if hit_target and not hit_stop else last["Close"]
            print(f"[이평회귀 매도] {symbol} {held['shares']}주 @ {exit_price:,.0f} ({reason})")
            broker.place_order(code, "sell", held["shares"])
            _record_trade(code, symbol, "sell", held["shares"], exit_price, note=f"auto:reversion {reason}",
                          extra={"사유": reason},
                          pnl_pct=(exit_price - held["avg_price"]) / held["avg_price"] * 100)
            return -1
        return 0

    if balance["positions"].get(code):
        return 0

    if not bool(last["reversion_entry"]):
        return 0
    if open_count >= max_positions:
        print(f"[건너뜀] {symbol}: 이평선 이격 신호지만 동시보유 한도({max_positions}종목) 도달")
        return 0

    try:
        strength = broker.get_execution_strength(code)
    except Exception as e:
        print(f"[건너뜀] {symbol}: 체결강도 조회 실패 ({e})")
        return 0
    if strength < params.exec_strength_threshold:
        print(f"[관찰] {symbol}: 이격도 {last['deviation_pct']:.1f}% 진입조건 충족했지만 "
              f"체결강도 {strength:.0f}% < {params.exec_strength_threshold:.0f}%")
        return 0

    shares = reversion_position_size(equity, last["Close"], last["atr"], params)
    if shares > 0:
        print(f"[이평회귀 매수] {symbol} {shares}주 @ {last['Close']:,.0f} "
              f"(이격도 {last['deviation_pct']:+.1f}%, 체결강도 {strength:.0f}%)")
        broker.place_order(code, "buy", shares)
        _record_trade(code, symbol, "buy", shares, last["Close"], note="auto:reversion",
                      extra={"이격도": f"{last['deviation_pct']:.1f}%", "체결강도": f"{strength:.0f}%"})
        return 1
    return 0


def _process_reversion_overseas(broker, symbol: str, exchange: str, balance: dict, equity: float, open_count: int,
                                 max_positions: int, params, reversion_held_codes: set) -> int:
    """해외 이평선회귀 처리. 체결강도는 KRX 전용 실시간 지표라 해외는 대응 API가 없어서
    이격도 조건만으로 진입한다."""
    from strategy.mean_reversion import compute_indicators as compute_reversion_signal
    from strategy.mean_reversion import position_size as reversion_position_size
    from strategy.mean_reversion import stop_price as reversion_stop_price
    from strategy.mean_reversion import take_profit_price as reversion_take_profit_price

    hist = compute_reversion_signal(load_history(symbol, period="1y"), params)
    last = hist.iloc[-1]

    live = get_live_quote(symbol)
    current_price = live["price"] if live else None
    session = live["session"] if live else None

    if symbol in reversion_held_codes:
        held = balance["positions"].get(symbol)
        if not held:
            return 0
        stop = reversion_stop_price(held["avg_price"], last["atr"], params)
        target = reversion_take_profit_price(held["avg_price"], params)
        if current_price is not None:
            hit_stop = current_price <= stop
            hit_target = current_price >= target
        else:
            hit_stop = last["Low"] <= stop
            hit_target = last["High"] >= target
        reverted = bool(last["reversion_exit"])
        if hit_stop or hit_target or reverted:
            reason = "손절" if hit_stop else ("익절" if hit_target else "이평선회귀")
            exit_price = current_price if current_price is not None else (
                target if hit_target and not hit_stop else last["Close"])
            session_tag = f" [{session}]" if session else ""
            print(f"[이평회귀-해외 매도]{session_tag} {symbol} {held['shares']}주 @ ${exit_price:,.2f} ({reason})")
            broker.place_order_overseas(symbol, "sell", held["shares"], exit_price, exchange)
            extra = {"사유": reason}
            if session:
                extra["세션"] = session
            _record_trade(symbol, symbol, "sell", held["shares"], exit_price, note=f"auto:reversion {reason}",
                          extra=extra,
                          pnl_pct=(exit_price - held["avg_price"]) / held["avg_price"] * 100)
            return -1
        return 0

    if balance["positions"].get(symbol):
        return 0

    if not bool(last["reversion_entry"]):
        return 0
    if open_count >= max_positions:
        print(f"[건너뜀] {symbol}: 이평선 이격 신호지만 동시보유 한도({max_positions}종목) 도달")
        return 0

    fill_price = current_price if current_price is not None else last["Close"]
    shares = reversion_position_size(equity, fill_price, last["atr"], params)
    if shares > 0:
        session_tag = f" [{session}]" if session else ""
        print(f"[이평회귀-해외 매수]{session_tag} {symbol} {shares}주 @ ${fill_price:,.2f} "
              f"(이격도 {last['deviation_pct']:+.1f}%)")
        broker.place_order_overseas(symbol, "buy", shares, fill_price, exchange)
        extra = {"이격도": f"{last['deviation_pct']:.1f}%"}
        if session:
            extra["세션"] = session
        _record_trade(symbol, symbol, "buy", shares, fill_price, note="auto:reversion", extra=extra)
        return 1
    return 0


def cmd_live(symbols: list | None = None, max_positions: int | None = None, capital: float | None = None):
    from broker.kis import KISBroker  # 백테스트만 쓸 때는 requests/자격증명이 필요 없도록 지연 임포트
    from webapp.overseas_symbols import SYMBOLS as OVERSEAS_SYMBOLS

    symbols = symbols if symbols is not None else CONFIG.symbols
    overseas_exchange = {s["code"]: s["exchange"] for s in OVERSEAS_SYMBOLS}
    max_positions = max_positions if max_positions is not None else CONFIG.risk.max_open_positions

    broker = KISBroker(CONFIG)
    balance = broker.get_balance()
    # capital을 지정하면 KIS 계좌의 실제 잔고 대신 이 명목 자산을 기준으로 포지션 사이징한다.
    # (계좌엔 실제 주문이 그대로 나가지만, "몇 주 살지" 계산의 기준 시드만 바꾸는 용도.
    # 이 모드에서는 equity가 매 실행마다 capital로 고정되므로 일일 손실 한도 체크는 사실상 무력화된다.)
    equity = capital if capital is not None else (
        balance["cash"] + sum(p["shares"] * p["avg_price"] for p in balance["positions"].values())
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

    kr_near_close = _is_kr_near_close()
    us_near_close = _is_us_near_close()

    print(f"[스캔] {len(symbols)}개 종목 확인, 동시보유 한도 {max_positions}종목 (현재 국내 {open_count}종목 보유 중)"
          f" | 국내 마감임박={kr_near_close} 해외 마감임박={us_near_close}")

    for symbol in symbols:
        if symbol.endswith((".KS", ".KQ")):
            delta = _process_domestic(broker, symbol, balance, equity, open_count, max_positions,
                                       near_close=kr_near_close)
        else:
            exchange = overseas_exchange.get(symbol, "NASD")
            if exchange not in overseas_balances:
                ob = broker.get_balance_overseas(exchange)
                ob["equity"] = ob["cash"] + sum(p["shares"] * p["avg_price"] for p in ob["positions"].values())
                overseas_balances[exchange] = ob
                open_count += len(ob["positions"])
            ob = overseas_balances[exchange]
            delta = _process_overseas(broker, symbol, exchange, ob, ob["equity"], open_count, max_positions,
                                       near_close=us_near_close)
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
    kr_near_close = _is_kr_near_close()
    us_near_close = _is_us_near_close()

    print(f"[모의 스캔] {len(symbols)}개 종목, 명목자본 {equity:,.0f} 기준, "
          f"동시보유 한도 {max_positions}종목 (현재 {open_count}종목 보유 중)"
          f" | 국내 마감임박={kr_near_close} 해외 마감임박={us_near_close}")

    for symbol in symbols:
        is_domestic = symbol.endswith((".KS", ".KQ"))
        code = symbol.split(".")[0] if is_domestic else symbol
        unit = "원" if is_domestic else "$"
        near_close = kr_near_close if is_domestic else us_near_close
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
            signal_exit = last["Low"] <= stop or bool(last["dead_cross"])
            if signal_exit or near_close:
                reason = "손절" if last["Low"] <= stop else ("데드크로스" if signal_exit else "장마감강제청산")
                print(f"[모의매도] {symbol} {held['shares']}주 @ {last['Close']:,.2f}{unit} ({chg_str}) ({reason})")
                db.add_trade(code, symbol, "sell", held["shares"], float(last["Close"]), note=note)
                del holdings[code]
                open_count -= 1
            continue

        if near_close:
            print(f"[건너뜀] {symbol} {last['Close']:,.2f}{unit} ({chg_str}) - 마감임박, 신규 진입 안 함")
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


def cmd_live_surge(symbols: list | None = None, symbols_us: list | None = None,
                    max_positions: int | None = None, capital: float | None = None):
    """급등주(전일 대비 등락률 + 거래량 급증 + 거래대금) 전략. 국내+해외 공통.
    각 시장 전량 매도 시점(국내 15:15 KST / 해외 04:00 KST)부터는 그 시장에서 신규 진입을
    멈추고, 당일 이 전략으로 산 포지션을 전량 강제청산한다 — 오버나이트 리스크를 피하는
    데이트레이딩 스타일이라서. 국내/해외 동시보유 한도는 각자 따로 적용된다."""
    from broker.kis import KISBroker
    from data.screener import fetch_us_day_gainers
    from webapp.overseas_symbols import SYMBOLS as OVERSEAS_SYMBOLS

    symbols = symbols if symbols is not None else CONFIG.surge_symbols
    if symbols_us is None:
        dynamic = fetch_us_day_gainers()
        symbols_us = list(dict.fromkeys(list(CONFIG.surge_symbols_us) + dynamic))
        if dynamic:
            print(f"[급등주-해외] 오늘의 급등주 스크리너 {len(dynamic)}종목 추가 스캔: {dynamic}")
    overseas_exchange = {s["code"]: s["exchange"] for s in OVERSEAS_SYMBOLS}
    max_positions = max_positions if max_positions is not None else CONFIG.risk.max_open_positions_surge
    params = CONFIG.surge

    broker = KISBroker(CONFIG)
    balance = broker.get_balance()
    equity_kr = capital if capital is not None else (
        balance["cash"] + sum(p["shares"] * p["avg_price"] for p in balance["positions"].values())
    )

    overseas_balances: dict[str, dict] = {}

    def get_ob(exchange: str) -> dict:
        if exchange not in overseas_balances:
            ob = broker.get_balance_overseas(exchange)
            ob["equity"] = capital if capital is not None else (
                ob["cash"] + sum(p["shares"] * p["avg_price"] for p in ob["positions"].values())
            )
            overseas_balances[exchange] = ob
        return overseas_balances[exchange]

    # ---------------- 국내 ----------------
    if _is_kr_near_close():
        print(f"[급등주-국내] 장마감 임박({EOD_FLATTEN_KR_HOUR:02d}:{EOD_FLATTEN_KR_MINUTE:02d} KST), "
              f"신규 진입 중단하고 당일 매수분 강제청산")
        _flatten_strategy_positions(broker, balance, "auto:surge", "급등주-국내")
    else:
        surge_held_kr = {
            c for c in _todays_strategy_buy_codes("auto:surge", "domestic")
            if balance["positions"].get(c, {}).get("shares", 0) > 0
        }
        open_count = len(surge_held_kr)
        print(f"[급등주-국내 스캔] {len(symbols)}개 종목, 동시보유 한도 {max_positions}종목 "
              f"(현재 {open_count}종목 보유 중)")
        for symbol in symbols:
            try:
                delta = _process_surge(broker, symbol, balance, equity_kr, open_count, max_positions,
                                        params, surge_held_kr)
            except Exception as e:
                print(f"[에러] {symbol}: {e}")
                continue
            open_count += delta

    # ---------------- 해외 ----------------
    if _is_us_near_close():
        print(f"[급등주-해외] 장마감 임박({EOD_FLATTEN_US_HOUR:02d}:{EOD_FLATTEN_US_MINUTE:02d} KST), "
              f"신규 진입 중단하고 당일 매수분 강제청산")
        _flatten_strategy_positions_overseas(broker, get_ob, overseas_exchange, "auto:surge", "급등주-해외")
    else:
        surge_held_us = set()
        for code in _todays_strategy_buy_codes("auto:surge", "overseas"):
            exchange = overseas_exchange.get(code, "NASD")
            if get_ob(exchange)["positions"].get(code, {}).get("shares", 0) > 0:
                surge_held_us.add(code)
        open_count_us = len(surge_held_us)
        print(f"[급등주-해외 스캔] {len(symbols_us)}개 종목, 동시보유 한도 {max_positions}종목 "
              f"(현재 {open_count_us}종목 보유 중)")
        for symbol in symbols_us:
            exchange = overseas_exchange.get(symbol, "NASD")
            ob = get_ob(exchange)
            try:
                delta = _process_surge_overseas(broker, symbol, exchange, ob, ob["equity"], open_count_us,
                                                 max_positions, params, surge_held_us)
            except Exception as e:
                print(f"[에러] {symbol}: {e}")
                continue
            open_count_us += delta


def cmd_live_reversion(symbols: list | None = None, symbols_us: list | None = None,
                        max_positions: int | None = None, capital: float | None = None):
    """20일 이동평균선 회귀(역추세) 전략. 이평선 대비 -3% 이상 빠지면 매수(국내는 체결강도
    100%↑ 조건 추가), 이평선 위로 다시 올라오면 매도. 국내/해외 공통, 다른 전략과 동일하게
    당일 전량 강제청산(EOD 플래튼)도 적용한다."""
    from broker.kis import KISBroker
    from webapp.overseas_symbols import SYMBOLS as OVERSEAS_SYMBOLS

    symbols = symbols if symbols is not None else CONFIG.reversion_symbols
    symbols_us = symbols_us if symbols_us is not None else CONFIG.reversion_symbols_us
    overseas_exchange = {s["code"]: s["exchange"] for s in OVERSEAS_SYMBOLS}
    max_positions = max_positions if max_positions is not None else CONFIG.risk.max_open_positions_reversion
    params = CONFIG.reversion

    broker = KISBroker(CONFIG)
    balance = broker.get_balance()
    equity_kr = capital if capital is not None else (
        balance["cash"] + sum(p["shares"] * p["avg_price"] for p in balance["positions"].values())
    )

    overseas_balances: dict[str, dict] = {}

    def get_ob(exchange: str) -> dict:
        if exchange not in overseas_balances:
            ob = broker.get_balance_overseas(exchange)
            ob["equity"] = capital if capital is not None else (
                ob["cash"] + sum(p["shares"] * p["avg_price"] for p in ob["positions"].values())
            )
            overseas_balances[exchange] = ob
        return overseas_balances[exchange]

    # ---------------- 국내 ----------------
    if _is_kr_near_close():
        print(f"[이평회귀-국내] 장마감 임박({EOD_FLATTEN_KR_HOUR:02d}:{EOD_FLATTEN_KR_MINUTE:02d} KST), "
              f"신규 진입 중단하고 당일 매수분 강제청산")
        _flatten_strategy_positions(broker, balance, "auto:reversion", "이평회귀-국내")
    else:
        reversion_held_kr = {
            c for c in _todays_strategy_buy_codes("auto:reversion", "domestic")
            if balance["positions"].get(c, {}).get("shares", 0) > 0
        }
        open_count = len(reversion_held_kr)
        print(f"[이평회귀-국내 스캔] {len(symbols)}개 종목, 동시보유 한도 {max_positions}종목 "
              f"(현재 {open_count}종목 보유 중)")
        for symbol in symbols:
            try:
                delta = _process_reversion(broker, symbol, balance, equity_kr, open_count, max_positions,
                                            params, reversion_held_kr)
            except Exception as e:
                print(f"[에러] {symbol}: {e}")
                continue
            open_count += delta

    # ---------------- 해외 ----------------
    if _is_us_near_close():
        print(f"[이평회귀-해외] 장마감 임박({EOD_FLATTEN_US_HOUR:02d}:{EOD_FLATTEN_US_MINUTE:02d} KST), "
              f"신규 진입 중단하고 당일 매수분 강제청산")
        _flatten_strategy_positions_overseas(broker, get_ob, overseas_exchange, "auto:reversion", "이평회귀-해외")
    else:
        reversion_held_us = set()
        for code in _todays_strategy_buy_codes("auto:reversion", "overseas"):
            exchange = overseas_exchange.get(code, "NASD")
            if get_ob(exchange)["positions"].get(code, {}).get("shares", 0) > 0:
                reversion_held_us.add(code)
        open_count_us = len(reversion_held_us)
        print(f"[이평회귀-해외 스캔] {len(symbols_us)}개 종목, 동시보유 한도 {max_positions}종목 "
              f"(현재 {open_count_us}종목 보유 중)")
        for symbol in symbols_us:
            exchange = overseas_exchange.get(symbol, "NASD")
            ob = get_ob(exchange)
            try:
                delta = _process_reversion_overseas(broker, symbol, exchange, ob, ob["equity"], open_count_us,
                                                     max_positions, params, reversion_held_us)
            except Exception as e:
                print(f"[에러] {symbol}: {e}")
                continue
            open_count_us += delta


def main():
    parser = argparse.ArgumentParser(description="주식 자동매매 프로그램")
    parser.add_argument("mode", nargs="?", default="backtest", choices=["backtest", "live", "surge", "reversion"])
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
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="live 모드에서 포지션 사이징에 쓸 명목 자산(원). 지정하면 KIS 계좌 실제 잔고 대신 "
             "이 값을 기준으로 매수 수량을 계산한다 (주문 자체는 그대로 KIS 계좌에 나간다).",
    )
    args = parser.parse_args()

    if args.mode == "backtest":
        cmd_backtest()
        return

    if args.mode == "surge":
        cmd_live_surge(max_positions=args.max_positions, capital=args.capital)
        return

    if args.mode == "reversion":
        cmd_live_reversion(max_positions=args.max_positions, capital=args.capital)
        return

    symbols = CONFIG.symbols
    if args.all_overseas:
        from webapp.overseas_symbols import SYMBOLS as OVERSEAS_SYMBOLS

        all_overseas = [s["code"] for s in OVERSEAS_SYMBOLS]
        symbols = list(dict.fromkeys(list(CONFIG.symbols) + all_overseas))  # 순서 유지 + 중복 제거
        print(f"[--all-overseas] 해외 큐레이션 종목 {len(all_overseas)}개 추가 스캔 (총 {len(symbols)}개)")

    if args.dry_run:
        cmd_live_dry_run(symbols, max_positions=args.max_positions, capital=args.capital, note=args.note)
    else:
        cmd_live(symbols, max_positions=args.max_positions, capital=args.capital)


if __name__ == "__main__":
    sys.exit(main())
