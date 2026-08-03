from abc import ABC, abstractmethod


class Broker(ABC):
    """실전/모의 브로커 공통 인터페이스. 전략·백테스트 코드는 이 인터페이스에만 의존하므로
    나중에 다른 증권사 API로 교체해도 상위 로직을 바꿀 필요가 없다."""

    @abstractmethod
    def get_price(self, symbol: str) -> float:
        ...

    @abstractmethod
    def get_balance(self) -> dict:
        """{'cash': float, 'positions': {symbol: {'shares': int, 'avg_price': float}}}"""
        ...

    @abstractmethod
    def place_order(self, symbol: str, side: str, shares: int) -> dict:
        """side: 'buy' | 'sell'. 주문 결과 딕셔너리를 반환한다."""
        ...
