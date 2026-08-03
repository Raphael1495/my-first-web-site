import math

import pandas as pd

from config import RiskLimits, StrategyParams


def compute_indicators(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    """이동평균(fast/slow)과 ATR(변동성)을 계산해 컬럼으로 추가한다."""
    out = df.copy()
    out["fast_ma"] = out["Close"].rolling(params.fast_ma).mean()
    out["slow_ma"] = out["Close"].rolling(params.slow_ma).mean()

    prev_close = out["Close"].shift(1)
    tr = pd.concat(
        [
            out["High"] - out["Low"],
            (out["High"] - prev_close).abs(),
            (out["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr"] = tr.rolling(params.atr_period).mean()

    out["trend_up"] = out["fast_ma"] > out["slow_ma"]
    out["golden_cross"] = out["trend_up"] & ~out["trend_up"].shift(1).fillna(False)
    out["dead_cross"] = ~out["trend_up"] & out["trend_up"].shift(1).fillna(False)
    return out


def position_size(
    capital: float,
    entry_price: float,
    atr: float,
    params: StrategyParams,
    risk: RiskLimits,
) -> int:
    """계좌 자산 대비 리스크(risk_per_trade)와 종목당 최대 비중(max_position_weight) 중
    더 보수적인 값으로 매수 수량을 정한다. 몰빵을 방지하기 위한 핵심 안전장치."""
    stop_distance = atr * params.atr_stop_multiple
    if stop_distance <= 0 or entry_price <= 0:
        return 0

    risk_amount = capital * params.risk_per_trade
    shares_by_risk = risk_amount / stop_distance

    max_notional = capital * risk.max_position_weight
    shares_by_weight = max_notional / entry_price

    return max(0, math.floor(min(shares_by_risk, shares_by_weight)))


def stop_price(entry_price: float, atr_at_entry: float, params: StrategyParams) -> float:
    return entry_price - atr_at_entry * params.atr_stop_multiple
