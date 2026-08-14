import math
from dataclasses import dataclass

import pandas as pd


@dataclass
class ReversionParams:
    ma_period: int = 20  # 기준 이동평균선 기간
    entry_deviation_pct: float = 3.0  # 이평선 대비 이만큼(%) 이상 밑으로 벌어지면 진입
    exec_strength_threshold: float = 100.0  # 체결강도(%) 최소 기준 — 실시간 API로만 확인 가능 (실거래에서만 적용)
    atr_period: int = 14
    atr_stop_multiple: float = 1.5  # 추세추종(2.0)보다 타이트하게 — 역추세 진입이라 리스크 관리가 더 중요
    risk_per_trade: float = 0.01
    max_position_weight: float = 0.15
    take_profit_pct: float = 0.05  # 이평선 회귀 전에 먼저 이만큼 오르면 안전하게 익절 (백업 청산 조건)


def compute_indicators(df: pd.DataFrame, params: ReversionParams) -> pd.DataFrame:
    """이동평균선과 그 대비 이격도(%)를 계산한다. 이격도가 -entry_deviation_pct 이하로
    벌어지면 진입 신호(reversion_entry), 종가가 이평선 위로 회귀하면 청산 신호(reversion_exit)다."""
    out = df.copy()
    out["ma"] = out["Close"].rolling(params.ma_period).mean()
    out["deviation_pct"] = (out["Close"] - out["ma"]) / out["ma"] * 100

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

    out["reversion_entry"] = out["deviation_pct"] <= -params.entry_deviation_pct
    out["reversion_exit"] = out["Close"] >= out["ma"]
    return out


def stop_price(entry_price: float, atr_at_entry: float, params: ReversionParams) -> float:
    return entry_price - atr_at_entry * params.atr_stop_multiple


def take_profit_price(entry_price: float, params: ReversionParams) -> float:
    return entry_price * (1 + params.take_profit_pct)


def position_size(capital: float, entry_price: float, atr: float, params: ReversionParams) -> int:
    """계좌 자산 대비 리스크(risk_per_trade)와 종목당 최대 비중(max_position_weight) 중
    더 보수적인 값으로 매수 수량을 정한다."""
    stop_distance = atr * params.atr_stop_multiple
    if stop_distance <= 0 or entry_price <= 0:
        return 0

    risk_amount = capital * params.risk_per_trade
    shares_by_risk = risk_amount / stop_distance

    max_notional = capital * params.max_position_weight
    shares_by_weight = max_notional / entry_price

    return max(0, math.floor(min(shares_by_risk, shares_by_weight)))
