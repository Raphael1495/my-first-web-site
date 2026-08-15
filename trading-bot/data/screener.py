"""야후 파이낸스 스크리너로 오늘 실제 급등/활발히 거래되는 미국 종목을 동적으로 찾는다.

급등주 전략의 대상 종목을 하루 단위로 고정해두면(어제 급등한 종목이 오늘도 급등한다는
보장이 없음) 전략 취지에 안 맞는다. Day Gainers 카테고리 하나만 보면 대형주 위주로만
잡히고 매일 겹치는 종목이 많길래, 소형주/거래량 중심 카테고리도 같이 섞어서 매일 종목
구성이 실제로 바뀌도록 한다."""
import yfinance as yf

SCREENER_CATEGORIES = ["day_gainers", "small_cap_gainers", "most_actives", "aggressive_small_caps"]


def fetch_us_surge_candidates(count_per_category: int = 25, min_price: float = 5.0) -> list[str]:
    """카테고리별로 조회해서 합친다(순서 유지, 중복 제거). 카테고리 하나가 실패해도
    나머지는 계속 시도한다 — 스크리너가 죽어도 큐레이션 유니버스로는 계속 스캔이
    돌아가야 하므로 예외를 카테고리별로 개별 삼킨다."""
    symbols: list[str] = []
    for category in SCREENER_CATEGORIES:
        try:
            res = yf.screen(category, count=count_per_category)
            symbols += [
                q["symbol"] for q in res.get("quotes", [])
                if q.get("symbol") and q.get("regularMarketPrice", 0) >= min_price
            ]
        except Exception as e:
            print(f"  ⚠️ 급등주 스크리너({category}) 조회 실패, 건너뜀: {e}")
    return list(dict.fromkeys(symbols))
