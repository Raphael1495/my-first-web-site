"""해외(미국) 종목 검색용 큐레이션 목록.

KRX처럼 공식 전체 상장목록을 한 번에 받아올 표준 API가 없어서, 자주 찾는 대형주
위주로 최소 목록만 담아둔다. 목록에 없는 티커도 검색창에 직접 입력하면 그대로
조회/거래는 된다 (자동완성만 안 될 뿐)."""

SYMBOLS = [
    {"code": "AAPL", "name": "Apple", "exchange": "NASD"},
    {"code": "MSFT", "name": "Microsoft", "exchange": "NASD"},
    {"code": "GOOGL", "name": "Alphabet(A)", "exchange": "NASD"},
    {"code": "AMZN", "name": "Amazon", "exchange": "NASD"},
    {"code": "NVDA", "name": "NVIDIA", "exchange": "NASD"},
    {"code": "TSLA", "name": "Tesla", "exchange": "NASD"},
    {"code": "META", "name": "Meta Platforms", "exchange": "NASD"},
    {"code": "AVGO", "name": "Broadcom", "exchange": "NASD"},
    {"code": "NFLX", "name": "Netflix", "exchange": "NASD"},
    {"code": "ADBE", "name": "Adobe", "exchange": "NASD"},
    {"code": "AMD", "name": "AMD", "exchange": "NASD"},
    {"code": "INTC", "name": "Intel", "exchange": "NASD"},
    {"code": "QCOM", "name": "Qualcomm", "exchange": "NASD"},
    {"code": "CSCO", "name": "Cisco", "exchange": "NASD"},
    {"code": "PEP", "name": "PepsiCo", "exchange": "NASD"},
    {"code": "COST", "name": "Costco", "exchange": "NASD"},
    {"code": "SBUX", "name": "Starbucks", "exchange": "NASD"},
    {"code": "JPM", "name": "JPMorgan Chase", "exchange": "NYSE"},
    {"code": "V", "name": "Visa", "exchange": "NYSE"},
    {"code": "MA", "name": "Mastercard", "exchange": "NYSE"},
    {"code": "UNH", "name": "UnitedHealth", "exchange": "NYSE"},
    {"code": "HD", "name": "Home Depot", "exchange": "NYSE"},
    {"code": "PG", "name": "Procter & Gamble", "exchange": "NYSE"},
    {"code": "DIS", "name": "Disney", "exchange": "NYSE"},
    {"code": "KO", "name": "Coca-Cola", "exchange": "NYSE"},
    {"code": "WMT", "name": "Walmart", "exchange": "NYSE"},
    {"code": "MCD", "name": "McDonald's", "exchange": "NYSE"},
    {"code": "NKE", "name": "Nike", "exchange": "NYSE"},
    {"code": "BA", "name": "Boeing", "exchange": "NYSE"},
    {"code": "XOM", "name": "ExxonMobil", "exchange": "NYSE"},
    {"code": "CVX", "name": "Chevron", "exchange": "NYSE"},
    {"code": "JNJ", "name": "Johnson & Johnson", "exchange": "NYSE"},
    {"code": "PFE", "name": "Pfizer", "exchange": "NYSE"},
    {"code": "T", "name": "AT&T", "exchange": "NYSE"},
    {"code": "VZ", "name": "Verizon", "exchange": "NYSE"},
    {"code": "BAC", "name": "Bank of America", "exchange": "NYSE"},
    {"code": "WFC", "name": "Wells Fargo", "exchange": "NYSE"},
    {"code": "GS", "name": "Goldman Sachs", "exchange": "NYSE"},
    {"code": "PYPL", "name": "PayPal", "exchange": "NASD"},
    {"code": "ORCL", "name": "Oracle", "exchange": "NYSE"},
    {"code": "CRM", "name": "Salesforce", "exchange": "NYSE"},
    {"code": "IBM", "name": "IBM", "exchange": "NYSE"},
]


def search_overseas_symbols(query: str, limit: int = 20) -> list:
    query = query.strip().lower()
    if not query:
        return []
    results = [s for s in SYMBOLS if query in s["name"].lower() or s["code"].lower().startswith(query)]
    if not results and query.isalpha():
        # 목록에 없어도 직접 입력한 티커는 그대로 선택 가능하게 해준다.
        results = [{"code": query.upper(), "name": query.upper(), "exchange": "NASD"}]
    return results[:limit]
