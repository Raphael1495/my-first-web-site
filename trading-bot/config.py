import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class StrategyParams:
    fast_ma: int = 20
    slow_ma: int = 60
    atr_period: int = 14
    atr_stop_multiple: float = 2.0
    risk_per_trade: float = 0.01  # 계좌 자산 대비 1건당 허용 손실 비율


@dataclass
class RiskLimits:
    max_position_weight: float = 0.2  # 종목당 최대 비중 (계좌 대비)
    max_daily_loss: float = 0.03  # 일일 최대 손실 비율, 초과 시 매매 중단
    max_open_positions: int = 5


@dataclass
class Config:
    symbols: list = field(default_factory=lambda: ["005930.KS", "AAPL"])  # 예시: 삼성전자, 애플
    backtest_period: str = "3y"
    initial_capital: float = 10_000_000.0
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskLimits = field(default_factory=RiskLimits)

    # KIS Developers 자격증명 (.env 에서 로드)
    kis_app_key: str = os.getenv("KIS_APP_KEY", "")
    kis_app_secret: str = os.getenv("KIS_APP_SECRET", "")
    kis_account_no: str = os.getenv("KIS_ACCOUNT_NO", "")  # "12345678-01" 형식
    # 해외주식 전용 계좌번호가 따로 있는 경우에만 채우면 됨. 비워두면 kis_account_no를 그대로 쓴다.
    kis_overseas_account_no: str = os.getenv("KIS_OVERSEAS_ACCOUNT_NO", "")
    kis_is_paper: bool = os.getenv("KIS_IS_PAPER", "true").lower() == "true"

    # 실전 주문을 실제로 내려면 이 값을 "YES"로 명시적으로 설정해야 함 (안전장치)
    confirm_live_trading: bool = os.getenv("CONFIRM_LIVE_TRADING", "") == "YES"


CONFIG = Config()
