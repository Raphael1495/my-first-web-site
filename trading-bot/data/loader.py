import pandas as pd
import yfinance as yf


def load_history(symbol: str, period: str = "3y", interval: str = "1d") -> pd.DataFrame:
    """symbol의 과거 OHLCV를 받아온다. 국내 종목은 '005930.KS'처럼 .KS/.KQ 접미사를 사용한다."""
    df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        raise ValueError(f"'{symbol}'에 대한 데이터를 가져오지 못했습니다. 종목 코드를 확인하세요.")
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.index.name = "date"
    return df
