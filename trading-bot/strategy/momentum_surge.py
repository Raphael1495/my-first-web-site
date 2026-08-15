import math
from dataclasses import dataclass

import pandas as pd


@dataclass
class SurgeParams:
    change_pct_threshold: float = 3.0  # 전일 종가 대비 등락률(%) 이 값 이상이어야 진입 (기존 7.0에서 완화)
    volume_multiple: float = 1.5  # 거래량이 최근 평균 대비 이 배수 이상이어야 진입 (기존 3.0에서 완화)
    volume_avg_period: int = 20  # 평균 거래량 계산 기간(일)
    min_trading_value: float = 5_000_000_000.0  # 최소 거래대금(원, 국내). 저유동성 종목 걸러내는 필터
    min_trading_value_usd: float = 300_000.0  # 최소 거래대금(달러, 해외). 소형주도 걸리게 완화(기존 300만→30만)
    atr_period: int = 14
    atr_stop_multiple: float = 1.2  # 추세추종(2.0)보다 타이트한 손절 — 급등주는 변동성이 커서 빨리 끊어야 함
    risk_per_trade: float = 0.01  # 계좌 자산 대비 1건당 허용 손실 비율
    max_position_weight: float = 0.1  # 종목당 최대 비중 — 추세추종(0.2)보다 보수적으로
    take_profit_pct: float = 0.05  # 종목당 익절 목표 — 진입가 대비 이만큼 오르면 그 자리에서 매도
    ma_period: int = 20  # 이격도 계산 기준 이동평균선 기간
    max_ma_deviation_pct: float = 25.0  # 20일선 대비 이만큼(%) 넘게 떠 있으면 "이미 너무 늘어난" 걸로 보고 진입 안 함
    # (오늘 하루 만에 급등한 경우도 그 시점 종가가 이미 반영되므로 똑같이 걸러진다)


def compute_surge_signal(df: pd.DataFrame, params: SurgeParams) -> pd.DataFrame:
    """전일 대비 등락률 + 거래량 급증 + 거래대금(유동성) 세 조건을 모두 만족하는 날을
    급등 진입 시그널로 표시한다."""
    out = df.copy()
    prev_close = out["Close"].shift(1)

    out["change_pct"] = (out["Close"] - prev_close) / prev_close * 100

    avg_volume = out["Volume"].rolling(params.volume_avg_period).mean().shift(1)
    out["volume_ratio"] = out["Volume"] / avg_volume

    out["trading_value"] = out["Close"] * out["Volume"]

    tr = pd.concat(
        [
            out["High"] - out["Low"],
            (out["High"] - prev_close).abs(),
            (out["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr"] = tr.rolling(params.atr_period).mean()

    ma = out["Close"].rolling(params.ma_period).mean()
    out["ma_deviation_pct"] = (out["Close"] - ma) / ma * 100

    out["surge_entry"] = (
        (out["change_pct"] >= params.change_pct_threshold)
        & (out["volume_ratio"] >= params.volume_multiple)
        & (out["trading_value"] >= params.min_trading_value)
        & (out["ma_deviation_pct"] <= params.max_ma_deviation_pct)
    )
    return out


def stop_price(entry_price: float, atr_at_entry: float, params: SurgeParams) -> float:
    return entry_price - atr_at_entry * params.atr_stop_multiple


def take_profit_price(entry_price: float, params: SurgeParams) -> float:
    return entry_price * (1 + params.take_profit_pct)


def position_size(capital: float, entry_price: float, atr: float, params: SurgeParams) -> int:
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
