"""한국투자증권(KIS Developers) REST API 어댑터.

⚠️ 중요: 아래 tr_id / 엔드포인트 값은 공개적으로 널리 알려진 값을 기반으로 작성했다.
국내주식 시세조회/잔고조회/주문(TR_ID_PRICE, TR_ID_BALANCE, TR_ID_ORDER)은 실제 모의투자
계좌로 이미 검증 완료했다 (2026-08). 반면 TR_ID_HOGA(호가)와 해외주식 관련 기능
(TR_ID_OVERSEAS_ORDER/place_order_overseas, TR_ID_OVERSEAS_BALANCE/get_balance_overseas)은
아직 실제로 테스트해본 적이 없으니, 사용 전 KIS Developers 포털에서 최신 문서와 대조
확인하고 모의투자로 먼저 검증할 것.
"""

import time

import requests

from broker.base import Broker
from config import Config

REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"

TR_ID_PRICE = "FHKST01010100"  # 검증됨
TR_ID_HOGA = "FHKST01010200"  # 미검증 (국내주식 현재가 호가/예상체결)
TR_ID_BALANCE = {"real": "TTC8434R", "paper": "VTTC8434R"}  # 검증됨
TR_ID_ORDER = {
    "real": {"buy": "TTC0802U", "sell": "TTC0801U"},
    "paper": {"buy": "VTTC0802U", "sell": "VTTC0801U"},
}  # 검증됨
TR_ID_OVERSEAS_ORDER = {
    "real": {"buy": "JTTT1002U", "sell": "JTTT1006U"},
    "paper": {"buy": "VTTT1002U", "sell": "VTTT1001U"},
}  # 미검증
TR_ID_OVERSEAS_BALANCE = {"real": "TTTS3012R", "paper": "VTTS3012R"}  # 미검증


class KISBroker(Broker):
    def __init__(self, config: Config):
        self.config = config
        self.base_url = PAPER_BASE_URL if config.kis_is_paper else REAL_BASE_URL
        self.mode = "paper" if config.kis_is_paper else "real"
        self._token = None
        self._token_expiry = 0

        if self.mode == "real" and not config.confirm_live_trading:
            raise RuntimeError(
                "실전투자 모드인데 CONFIRM_LIVE_TRADING=YES 가 설정되어 있지 않습니다. "
                "실수로 실전 주문이 나가는 것을 막기 위한 안전장치입니다. "
                ".env 에서 명시적으로 설정하세요."
            )

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        resp = requests.post(
            f"{self.base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.config.kis_app_key,
                "appsecret": self.config.kis_app_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 86400)) - 60
        return self._token

    def _headers(self, tr_id: str) -> dict:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._get_token()}",
            "appkey": self.config.kis_app_key,
            "appsecret": self.config.kis_app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def get_price(self, symbol: str) -> float:
        resp = requests.get(
            f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=self._headers(TR_ID_PRICE),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["output"]["stck_prpr"])

    def get_hoga(self, symbol: str) -> dict:
        """국내주식 매수/매도 10호가 스냅샷 (실시간 아님, 호출 시점 기준 조회)."""
        resp = requests.get(
            f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            headers=self._headers(TR_ID_HOGA),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["output1"]

    def _overseas_account_parts(self) -> tuple[str, str]:
        """해외주식 전용 계좌번호(KIS_OVERSEAS_ACCOUNT_NO)가 설정돼 있으면 그걸 쓰고,
        없으면 국내와 같은 계좌번호(kis_account_no)를 그대로 쓴다."""
        account_no = self.config.kis_overseas_account_no or self.config.kis_account_no
        return tuple(account_no.split("-"))

    def get_balance(self) -> dict:
        cano, prdt_cd = self.config.kis_account_no.split("-")
        resp = requests.get(
            f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=self._headers(TR_ID_BALANCE[self.mode]),
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        positions = {
            item["pdno"]: {"shares": int(item["hldg_qty"]), "avg_price": float(item["pchs_avg_pric"])}
            for item in body.get("output1", [])
            if int(item["hldg_qty"]) > 0
        }
        cash = float(body["output2"][0]["dnca_tot_amt"]) if body.get("output2") else 0.0
        return {"cash": cash, "positions": positions}

    def place_order(self, symbol: str, side: str, shares: int) -> dict:
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        cano, prdt_cd = self.config.kis_account_no.split("-")
        resp = requests.post(
            f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash",
            headers=self._headers(TR_ID_ORDER[self.mode][side]),
            json={
                "CANO": cano,
                "ACNT_PRDT_CD": prdt_cd,
                "PDNO": symbol,
                "ORD_DVSN": "01",  # 01 = 시장가
                "ORD_QTY": str(shares),
                "ORD_UNPR": "0",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_balance_overseas(self, exchange: str = "NASD", currency: str = "USD") -> dict:
        """해외주식 잔고조회 (예수금 + 보유종목). ⚠️ 미검증 — 모의투자로 먼저 확인할 것.
        거래소(exchange)별로 따로 조회해야 한다 (국내처럼 통합조회가 아님)."""
        cano, prdt_cd = self._overseas_account_parts()
        resp = requests.get(
            f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance",
            headers=self._headers(TR_ID_OVERSEAS_BALANCE[self.mode]),
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": prdt_cd,
                "OVRS_EXCG_CD": exchange,
                "TR_CRCY_CD": currency,
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        positions = {
            item["ovrs_pdno"]: {
                "shares": int(float(item["ovrs_cblc_qty"])),
                "avg_price": float(item["pchs_avg_pric"]),
            }
            for item in body.get("output1", [])
            if float(item.get("ovrs_cblc_qty", 0)) > 0
        }
        output2 = body.get("output2") or {}
        if isinstance(output2, list):  # 응답 형태가 리스트로 올 수도 있어 방어적으로 처리
            output2 = output2[0] if output2 else {}
        cash = float(output2.get("frcr_dncl_amt_2", 0) or 0)
        return {"cash": cash, "positions": positions}

    def place_order_overseas(self, symbol: str, side: str, shares: int, price: float, exchange: str = "NASD") -> dict:
        """해외주식 주문. ⚠️ 미검증 — 모의투자로 먼저 확인할 것.
        해외는 국내와 달리 지정가 주문이 기본이라 price(주문 단가)가 필요하다.
        exchange 예: NASD(나스닥), NYSE(뉴욕), AMEX(아멕스)."""
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        cano, prdt_cd = self._overseas_account_parts()
        resp = requests.post(
            f"{self.base_url}/uapi/overseas-stock/v1/trading/order",
            headers=self._headers(TR_ID_OVERSEAS_ORDER[self.mode][side]),
            json={
                "CANO": cano,
                "ACNT_PRDT_CD": prdt_cd,
                "OVRS_EXCG_CD": exchange,
                "PDNO": symbol,
                "ORD_QTY": str(shares),
                "OVRS_ORD_UNPR": str(price),
                "ORD_SVR_DVSN_CD": "0",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
