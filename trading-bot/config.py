import os
from dataclasses import dataclass, field

import truststore
from dotenv import load_dotenv

from strategy.mean_reversion import ReversionParams
from strategy.momentum_surge import SurgeParams
from webapp.overseas_symbols import SYMBOLS as _OVERSEAS_SYMBOLS

# 사내망 SSL 검사 프록시(Hyundai 루트 CA)가 재서명한 인증서를 certifi가 신뢰하지 못해
# 발생하는 CERTIFICATE_VERIFY_FAILED 대응 — OpenSSL 대신 Windows OS 인증서 저장소로 검증.
# (이 컴퓨터/사내망 전용 조치라 다른 컴퓨터의 config.py에는 없어도 정상입니다.)
truststore.inject_into_ssl()

load_dotenv()


@dataclass
class StrategyParams:
    fast_ma: int = 20
    slow_ma: int = 60
    atr_period: int = 14
    atr_stop_multiple: float = 2.0
    risk_per_trade: float = 0.01  # 계좌 자산 대비 1건당 허용 손실 비율
    take_profit_pct: float = 0.05  # 종목당 익절 목표 — 진입가 대비 이만큼 오르면 그 자리에서 매도


@dataclass
class RiskLimits:
    max_position_weight: float = 0.2  # 종목당 최대 비중 (계좌 대비)
    max_daily_loss: float = 0.03  # 일일 최대 손실 비율, 초과 시 매매 중단
    max_open_positions: int = 5
    max_open_positions_surge: int = 3  # 급등주 전략 동시보유 한도 (추세추종보다 보수적으로)
    max_open_positions_reversion: int = 3  # 이평선회귀 전략 동시보유 한도 (역추세 진입이라 보수적으로)


# 급등주 스캔용 종목 유니버스. 매 사이클마다 종목당 네트워크 조회가 1번씩 필요해서
# 전체 상장종목을 실시간으로 훑는 건 현실적으로 느리다 — 유동성 좋은 코스피 대형주로
# 범위를 제한해서 5분 간격 폴링에서도 돌아가게 한다.
SURGE_UNIVERSE = [
    "005930.KS", "000660.KS", "035420.KS", "035720.KS", "005380.KS", "000270.KS",
    "051910.KS", "006400.KS", "105560.KS", "055550.KS", "012330.KS", "096770.KS",
    "017670.KS", "030200.KS", "033780.KS", "015760.KS", "032830.KS", "086790.KS",
    "316140.KS", "024110.KS", "005490.KS", "010130.KS", "011200.KS", "010950.KS",
    "329180.KS", "042660.KS", "009540.KS", "011070.KS", "066570.KS", "003550.KS",
    "034730.KS", "018260.KS", "028260.KS", "010140.KS", "011780.KS", "009830.KS",
    "000720.KS", "047810.KS", "079550.KS", "012450.KS", "068270.KS", "207940.KS",
    "326030.KS", "090430.KS", "051900.KS", "352820.KS", "259960.KS", "036570.KS",
    "251270.KS", "086520.KS", "247540.KS", "373220.KS", "066970.KS",
]

# 급등주 스캔용 해외(미국) 유니버스 — 관심종목 검색 큐레이션 목록을 그대로 재사용한다.
# 대형 우량주(관심종목 큐레이션) + 변동성 큰 소형주 몇 개를 더해서 실제로 급등 신호가
# 걸릴 확률을 높인다 (대형주만으로는 +7%/거래량 급증 조건이 거의 안 걸림).
SURGE_UNIVERSE_US = [s["code"] for s in _OVERSEAS_SYMBOLS] + ["DFSC", "FGI"]

# 이 시각부터 신규 진입을 멈추고, 당일 매수분은 전량 강제청산한다 (모두 한국시간/Asia/Seoul 기준).
# 써머타임 기준: 국내장 09:00~15:20, 해외(미국)장 10:30~익일 05:00.
EOD_FLATTEN_KR_HOUR = 15      # 국내(KRX) 전량 매도 시점 15:15
EOD_FLATTEN_KR_MINUTE = 15
EOD_FLATTEN_US_HOUR = 4       # 해외(미국) 전량 매도 시점 04:00 (한국시간, 익일)
EOD_FLATTEN_US_MINUTE = 0

# 하위 호환용 별칭 (기존 코드/다른 컴퓨터 브랜치가 이 이름을 참조할 수 있음)
SURGE_EOD_FLATTEN_HOUR = EOD_FLATTEN_KR_HOUR
SURGE_EOD_FLATTEN_MINUTE = EOD_FLATTEN_KR_MINUTE


@dataclass
class Config:
    symbols: list = field(default_factory=lambda: [
        "105560.KS",  # KB금융
        "096770.KS",  # SK이노베이션
        "068270.KS",  # 셀트리온
        "090430.KS",  # 아모레퍼시픽
        "011200.KS",  # HMM
    ])  # 8/10 모의투자로 실제 보유 중인 국내 종목 (오늘은 국내만, 해외 제외)
    backtest_period: str = "3y"
    initial_capital: float = 10_000_000.0
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskLimits = field(default_factory=RiskLimits)
    surge: SurgeParams = field(default_factory=SurgeParams)
    surge_symbols: list = field(default_factory=lambda: list(SURGE_UNIVERSE))
    surge_symbols_us: list = field(default_factory=lambda: list(SURGE_UNIVERSE_US))
    reversion: ReversionParams = field(default_factory=ReversionParams)
    # 급등주와 같은 유동성 좋은 유니버스를 재사용 (이평선회귀도 대형주 대상 역추세 전략이라 적합)
    reversion_symbols: list = field(default_factory=lambda: list(SURGE_UNIVERSE))
    reversion_symbols_us: list = field(default_factory=lambda: list(SURGE_UNIVERSE_US))

    # KIS Developers 자격증명 (.env 에서 로드)
    kis_app_key: str = os.getenv("KIS_APP_KEY", "")
    kis_app_secret: str = os.getenv("KIS_APP_SECRET", "")
    kis_account_no: str = os.getenv("KIS_ACCOUNT_NO", "")  # "12345678-01" 형식
    # 해외주식 전용 계좌번호가 따로 있는 경우에만 채우면 됨. 비워두면 kis_account_no를 그대로 쓴다.
    kis_overseas_account_no: str = os.getenv("KIS_OVERSEAS_ACCOUNT_NO", "")
    kis_is_paper: bool = os.getenv("KIS_IS_PAPER", "true").lower() == "true"

    # 실전 주문을 실제로 내려면 이 값을 "YES"로 명시적으로 설정해야 함 (안전장치)
    confirm_live_trading: bool = os.getenv("CONFIRM_LIVE_TRADING", "") == "YES"

    # 매수/매도 체결 시 텔레그램 알림 (.env 에서 로드, 없으면 알림 기능 자체가 꺼짐)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")


CONFIG = Config()
