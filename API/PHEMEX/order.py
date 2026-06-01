import time
import json
import hmac
import hashlib
from typing import Any, Dict, Optional
import aiohttp
from utils import float_to_str

class PhemexPrivateClient:
    BASE_URL = "https://api.phemex.com"

    def __init__(self, api_key: str, api_secret: str, session: aiohttp.ClientSession):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = session

    def _get_signature(self, path: str, query_no_question: str, expiry: int, body_str: str) -> str:
        # Строго как в твоем рабочем скрипте: query_no_question БЕЗ '?' в начале
        message = f"{path}{query_no_question}{expiry}{body_str}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    async def _request(self, method: str, path: str, query_no_q: str = "", body: Optional[Dict[str, Any]] = None, timeout_sec: float = 10.0) -> Dict[str, Any]:
        expiry = int(time.time() + 60)
        body_str = json.dumps(body, separators=(',', ':')) if body else ""
        
        # Получаем подпись по правильной строке (без ?)
        signature = self._get_signature(path, query_no_q, expiry, body_str)
        
        headers = {
            "Content-Type": "application/json",
            "x-phemex-access-token": self.api_key,
            "x-phemex-request-expiry": str(expiry),
            "x-phemex-request-signature": signature
        }

        # Для URL добавляем '?', если query_no_q не пустой
        query_for_url = f"?{query_no_q}" if query_no_q else ""
        url = f"{self.BASE_URL}{path}{query_for_url}"
        
        async with self.session.request(method, url, headers=headers, data=body_str if body else None, timeout=timeout_sec) as resp:
            text = await resp.text()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(f"Bad response {resp.status}: {text}")

    async def place_limit_order(self, symbol: str, side: str, qty: float, price: float, pos_side: str) -> Dict[str, Any]:
        
        body = {
            "symbol": symbol,
            "side": side,
            "orderQtyRq": float_to_str(qty),
            "priceRp": float_to_str(price),
            "ordType": "Limit",
            "timeInForce": "GoodTillCancel",
            "posSide": pos_side # Так как мы используем hedged mode, posSide обязателен
        }
        
        # Для ордера query пустой, данные идут в теле
        return await self._request("POST", "/g-orders", body=body)

    async def cancel_order(self, symbol: str, order_id: str, pos_side: str) -> Dict[str, Any]:
        # В Hedge режиме биржа строго требует передавать posSide при отмене
        query_no_q = f"orderID={order_id}&posSide={pos_side}&symbol={symbol}"
        return await self._request("DELETE", "/g-orders/cancel", query_no_q=query_no_q)