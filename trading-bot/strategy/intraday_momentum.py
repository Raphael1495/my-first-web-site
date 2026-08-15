"""장중 상위 급등주를 분봉으로 감시하다가 "급락 → 바닥다짐 → 매수세 유입" 패턴에서
진입하고, 상승 모멘텀이 꺾이면 바로 청산하는 당일 초단기 스캘핑 전략.

momentum_surge.py(일봉 기반, 하루 단위 등락률로 진입)와 달리 이 전략은 진입/청산
판단을 전부 1분봉으로 한다 — 급등주는 하루 동안 오르기만 하는 게 아니라 오르내림을
반복하므로, 그 안에서 눌림목(조정) 후 재상승하는 타이밍을 잡는 게 목적이다.
"""
import math
from dataclasses import dataclass

import pandas as pd


@dataclass
class IntradayParams:
    lookback_minutes: int = 60  # 패턴 탐지에 쓸 최근 분봉 구간
    drop_pct: float = 3.0  # 직전 고점 대비 이만큼(%) 이상 빠져야 "급락"으로 인정
    base_minutes: int = 3  # 바닥을 다졌다고 볼 최소 연속 분봉 수
    base_range_pct: float = 1.5  # 그 구간 고저폭이 이 이내여야 "다지고 있다"로 판단
    breakout_volume_mult: float = 2.0  # 바닥 구간 평균 거래량 대비 이 배수 이상 터져야 "매수세 유입"
    stall_minutes: int = 3  # 신고가 갱신 없이 이만큼(분) 지나면 "상승 모멘텀 정체"로 보고 청산
    stop_loss_pct: float = 2.0  # 진입가 대비 이만큼 반대로 가면 안전장치로 강제 손절
    take_profit_pct: float = 4.0  # 진입가 대비 이만큼 오르면 모멘텀 정체를 기다리지 않고 바로 익절
    risk_per_trade: float = 0.01  # 계좌 자산 대비 1건당 허용 손실 비율
    max_position_weight: float = 0.1  # 종목당 최대 비중


def position_size(capital: float, entry_price: float, params: IntradayParams) -> int:
    """손절폭이 고정 %(stop_loss_pct)라서 ATR 대신 그 비율로 리스크 기준 수량을 정한다."""
    if entry_price <= 0:
        return 0
    stop_distance = entry_price * params.stop_loss_pct / 100
    if stop_distance <= 0:
        return 0

    risk_amount = capital * params.risk_per_trade
    shares_by_risk = risk_amount / stop_distance

    max_notional = capital * params.max_position_weight
    shares_by_weight = max_notional / entry_price

    return max(0, math.floor(min(shares_by_risk, shares_by_weight)))


def find_entry_signal(df: pd.DataFrame, params: IntradayParams) -> dict | None:
    """최근 분봉에서 "급락 → 바닥다짐 → 매수세 유입" 패턴을 찾는다. 없으면 None.
    df는 최소 1분 간격 OHLCV, 최신 봉이 마지막 행이어야 한다."""
    if len(df) < params.lookback_minutes // 2:
        return None

    recent = df.tail(params.lookback_minutes)
    high_idx = recent["High"].idxmax()
    recent_high = recent.loc[high_idx, "High"]

    after_high = recent.loc[high_idx:]
    if len(after_high) < params.base_minutes + 2:
        return None  # 고점 이후 아직 데이터가 부족 (급락+다짐할 시간이 안 지남)

    low_after_high = after_high["Low"].min()
    drop_pct = (low_after_high - recent_high) / recent_high * 100
    if drop_pct > -params.drop_pct:
        return None  # 충분히 빠지지 않음 — 아직 "파티" 초입일 수 있음

    base = after_high.iloc[:-1].tail(params.base_minutes)  # 지금 막 들어온 마지막 봉(돌파 후보)은 제외하고 다짐 구간만 본다
    if len(base) < params.base_minutes:
        return None
    base_high = base["High"].max()
    base_low = base["Low"].min()
    base_range_pct = (base_high - base_low) / base_low * 100
    if base_range_pct > params.base_range_pct:
        return None  # 아직 흔들리는 중 — 다짐이 안 됨

    base_avg_volume = base["Volume"].mean()
    last = df.iloc[-1]
    if base_avg_volume <= 0 or last["Volume"] < base_avg_volume * params.breakout_volume_mult:
        return None  # 거래량 안 터짐 — 매수세 유입 신호 없음
    if last["Close"] <= base_high:
        return None  # 아직 바닥 구간 고점을 못 뚫음

    return {
        "entry_price": float(last["Close"]),
        "recent_high": float(recent_high),
        "drop_pct": float(drop_pct),
        "base_high": float(base_high),
        "base_low": float(base_low),
        "volume_mult": float(last["Volume"] / base_avg_volume),
    }


def check_exit_signal(df: pd.DataFrame, entry_price: float, entry_index, params: IntradayParams) -> tuple[bool, str] | None:
    """진입 이후 청산 시점인지 판단한다. (True, 사유) 또는 청산 아니면 None."""
    last = df.iloc[-1]

    target = entry_price * (1 + params.take_profit_pct / 100)
    if last["High"] >= target:
        return True, "익절"

    stop = entry_price * (1 - params.stop_loss_pct / 100)
    if last["Low"] <= stop:
        return True, "손절"

    since_entry = df.loc[entry_index:]
    if len(since_entry) <= params.stall_minutes:
        return None  # 진입한 지 얼마 안 지남 — 아직 정체 판단 이름

    recent = since_entry.tail(params.stall_minutes)
    running_high_before = since_entry["High"].iloc[: -params.stall_minutes].max() \
        if len(since_entry) > params.stall_minutes else entry_price
    if recent["High"].max() <= running_high_before:
        return True, "모멘텀정체"

    return None
