"""야후 파이낸스 'Day Gainers' 스크리너로 오늘 실제 급등 중인 미국 종목을 동적으로 찾는다.

급등주 전략의 대상 종목을 하루 단위로 고정해두면(어제 급등한 종목이 오늘도 급등한다는
보장이 없음) 전략 취지에 안 맞아서, 스캔마다 그날 실제 등락률 상위 종목을 가져와
후보로 섞어 쓴다."""
import yfinance as yf


def fetch_us_day_gainers(count: int = 25, min_price: float = 5.0) -> list[str]:
    """실패하거나 결과가 없으면 빈 리스트를 준다 — 스크리너가 죽어도 나머지 큐레이션
    유니버스로는 계속 스캔이 돌아가야 하므로 예외를 여기서 삼킨다."""
    try:
        res = yf.screen("day_gainers", count=count)
        return [
            q["symbol"] for q in res.get("quotes", [])
            if q.get("symbol") and q.get("regularMarketPrice", 0) >= min_price
        ]
    except Exception as e:
        print(f"  ⚠️ 급등주 스크리너 조회 실패, 큐레이션 유니버스만 사용: {e}")
        return []
