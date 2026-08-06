"""KRX 상장종목 코드/이름 검색. 최초 1회 refresh_symbol_cache()로 전체 목록을
로컬에 캐싱해두고, 이후에는 캐시 파일에서 바로 검색한다 (네트워크 불필요)."""

import json
from pathlib import Path

CACHE_FILE = Path(__file__).parent / "krx_symbols_cache.json"

# 캐시가 아직 없을 때 쓰는 최소 목록 (대형주 위주). refresh_symbol_cache()로 갱신 권장.
_FALLBACK_SYMBOLS = [
    {"code": "005930", "name": "삼성전자", "market": "KOSPI"},
    {"code": "000660", "name": "SK하이닉스", "market": "KOSPI"},
    {"code": "035420", "name": "NAVER", "market": "KOSPI"},
    {"code": "035720", "name": "카카오", "market": "KOSPI"},
    {"code": "005380", "name": "현대차", "market": "KOSPI"},
    {"code": "000270", "name": "기아", "market": "KOSPI"},
    {"code": "051910", "name": "LG화학", "market": "KOSPI"},
    {"code": "006400", "name": "삼성SDI", "market": "KOSPI"},
    {"code": "105560", "name": "KB금융", "market": "KOSPI"},
    {"code": "055550", "name": "신한지주", "market": "KOSPI"},
]


def refresh_symbol_cache() -> int:
    """KRX 전체 상장종목 목록을 받아와 로컬 캐시 파일에 저장한다. 네트워크 필요."""
    import FinanceDataReader as fdr

    df = fdr.StockListing("KRX")
    symbols = [
        {"code": str(row["Code"]), "name": str(row["Name"]), "market": str(row.get("Market", ""))}
        for _, row in df.iterrows()
    ]
    CACHE_FILE.write_text(json.dumps(symbols, ensure_ascii=False))
    return len(symbols)


def load_symbols() -> list:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return _FALLBACK_SYMBOLS


def search_symbols(query: str, limit: int = 20) -> list:
    query = query.strip().lower()
    if not query:
        return []
    results = [
        s for s in load_symbols()
        if query in s["name"].lower() or s["code"].startswith(query)
    ]
    return results[:limit]
