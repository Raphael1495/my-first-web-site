"""한국투자증권(KIS Developers) REST API 어댑터.

⚠️ 중요: 아래 tr_id / 엔드포인트 값은 공개적으로 널리 알려진 값을 기반으로 작성했지만,
KIS Developers 포털(https://apiportal.koreainvestment.com)에서 최신 문서와
반드시 대조 확인한 뒤 사용해야 한다. 특히 실전투자 주문을 실행하기 전에는
모의투자(KIS_IS_PAPER=true) 계좌로 충분히 검증할 것.

해외주식 주문은 아직 구현하지 않았다 (place_order_overseas 참고, tr_id 확인 후 추가 필요).
"""

import time

import requests

from broker.base import Broker
from config import Config

REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"

TR_ID_PRICE = "FHKST01010100"
TR_ID_BALANCE = {"real": "TTC8434R", "paper": "VTTC8434R"}
TR_ID_ORDER = {
    "real": {"buy": "TTC0802U", "sell": "TTC0801U"},
    "paper": {"buy": "VTTC0802U", "sell": "VTTC0801U"},
}


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

    def place_order_overseas(self, *args, **kwargs):
        raise NotImplementedError(
            "해외주식 주문은 아직 구현되지 않았습니다. KIS Developers 문서에서 "
            "해외주식 주문 tr_id/엔드포인트를 확인한 뒤 구현하세요."
        )
