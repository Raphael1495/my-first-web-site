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
