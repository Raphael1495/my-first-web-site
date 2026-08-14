import pandas as pd
import yfinance as yf


def load_history(symbol: str, period: str = "3y", interval: str = "1d") -> pd.DataFrame:
    """symbol의 과거 OHLCV를 받아온다. 국내 종목은 '005930.KS'처럼 .KS/.KQ 접미사를 사용한다."""
    df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        raise ValueError(f"'{symbol}'에 대한 데이터를 가져오지 못했습니다. 종목 코드를 확인하세요.")
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    # 장 시작 전이나 데이터 갱신 지연으로 당일 봉이 OHLC 없이 NaN으로만 들어오는 경우가 있어서 제거한다
    # (NaN이 그대로 남으면 JSON 직렬화가 실패해 API가 500을 낸다).
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty:
        raise ValueError(f"'{symbol}'에 대한 데이터를 가져오지 못했습니다. 종목 코드를 확인하세요.")
    df.index.name = "date"
    return df


def get_live_quote(symbol: str) -> dict | None:
    """정규장/프리마켓/애프터마켓 중 타임스탬프가 가장 최근인 실제 체결가를 돌려준다.
    해외종목 전용 — 일봉(load_history)은 정규장 마감가만 담고 있어서, 지금 이 순간 실제로
    낼 수 있는 체결가(세션 구분 포함)가 필요한 실시간 매수/매도 판단에 이 함수를 쓴다.
    조회 실패하거나 값이 없으면 None (호출부는 일봉 기준 로직으로 폴백해야 한다)."""
    try:
        info = yf.Ticker(symbol).info
    except Exception:
        return None
    candidates = []
    for price_key, time_key, session in (
        ("regularMarketPrice", "regularMarketTime", "정규장"),
        ("postMarketPrice", "postMarketTime", "애프터마켓"),
        ("preMarketPrice", "preMarketTime", "프리마켓"),
    ):
        price, ts = info.get(price_key), info.get(time_key)
        if price is not None and ts:
            candidates.append((ts, float(price), session))
    if not candidates:
        return None
    ts, price, session = max(candidates, key=lambda c: c[0])

    # "전일종가" 기준점은 세션에 따라 다르다. 정규장/애프터마켓일 때는 오늘 정규장이 이미
    # 열렸으니 regularMarketPrice = 오늘 값이고 regularMarketPreviousClose(어제 종가)가 맞다.
    # 하지만 프리마켓(아직 오늘 정규장 시작 전)일 땐 regularMarketPrice 자체가 "마지막으로
    # 끝난 정규장"의 종가, 즉 어제 종가다 — 이때 regularMarketPreviousClose를 쓰면 그저께
    # 종가와 비교하는 꼴이 되어 등락률이 하루 어긋난다.
    if session == "프리마켓" and info.get("regularMarketPrice") is not None:
        prev_close = float(info["regularMarketPrice"])
    else:
        prev_close = float(info.get("regularMarketPreviousClose") or price)
    return {"price": price, "session": session, "prev_close": prev_close}
