"""매수/매도 체결 시 텔레그램으로 알림을 보낸다.

설정 안 돼 있거나(.env에 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 없음) 전송이 실패해도
예외를 삼켜서 매매 로직에는 절대 영향을 주지 않는다 — 알림은 어디까지나 부가 기능이다.
"""
import requests

from config import CONFIG


def send_trade_alert(code: str, name: str, side: str, shares: int, price: float, extra: dict | None = None):
    """extra는 종목명/코드/수량/단가 아래에 "키 : 값" 형식으로 덧붙일 상세 정보
    (예: {"사유": "익절"}, {"체결강도": "101%"})."""
    if not CONFIG.telegram_bot_token or not CONFIG.telegram_chat_id:
        return

    side_label = "매수" if side == "buy" else "매도"
    emoji = "🟢" if side == "buy" else "🔴"
    domestic = code.isdigit()
    price_label = "매수단가" if side == "buy" else "매도단가"
    price_str = f"{price:,.0f}원" if domestic else f"${price:,.2f}"

    lines = [
        f"{emoji} [{side_label}]",
        f"종목명 : {name}",
        f"코드 : {code}",
        f"수량 : {shares}주",
        f"{price_label} : {price_str}",
    ]
    for key, value in (extra or {}).items():
        lines.append(f"{key} : {value}")
    text = "\n".join(lines)

    try:
        requests.post(
            f"https://api.telegram.org/bot{CONFIG.telegram_bot_token}/sendMessage",
            json={"chat_id": CONFIG.telegram_chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"  ⚠️ 텔레그램 알림 실패: {e}")
