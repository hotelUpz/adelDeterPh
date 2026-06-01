# ============================================================
# FILE: API/PHEMEX/get_pos_symbols.py
# ROLE: Резервный REST клиент для определения активных позиций/ордеров.
# python -m API.PHEMEX.get_pos_symbols
# ============================================================

import time
import json
import hmac
import hashlib
import asyncio
from typing import Any, Dict, Optional, Set
import aiohttp
from c_log import UnifiedLogger

logger = UnifiedLogger("api")

class PhemexPrivateRESTFallback:
    BASE_URL = "https://api.phemex.com"

    def __init__(self, api_key: str, api_secret: str, session: aiohttp.ClientSession, retries: int = 2):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = session
        self.retries = retries

    def _get_signature(self, path: str, query_no_question: str, expiry: int, body_str: str) -> str:
        message = f"{path}{query_no_question}{expiry}{body_str}"
        return hmac.new(self.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    async def _request(self, method: str, path: str, query_no_q: str = "", body: Optional[Dict[str, Any]] = None, timeout_sec: float = 10.0) -> Dict[str, Any]:
        query_for_url = f"?{query_no_q}" if query_no_q else ""
        url = f"{self.BASE_URL}{path}{query_for_url}"
        body_str = json.dumps(body, separators=(',', ':')) if body else ""
        
        attempts = self.retries if method.upper() in ("GET", "DELETE", "PUT") else 1
        last_err = None

        for attempt in range(1, attempts + 1):
            try:
                expiry = int(time.time() + 60)
                signature = self._get_signature(path, query_no_q, expiry, body_str)
                headers = {
                    "Content-Type": "application/json",
                    "x-phemex-access-token": self.api_key,
                    "x-phemex-request-expiry": str(expiry),
                    "x-phemex-request-signature": signature
                }
                async with self.session.request(method, url, headers=headers, data=body_str if body else None, timeout=timeout_sec) as resp:
                    text = await resp.text()
                    if resp.status not in (200, 201, 202, 204):
                        if resp.status == 401 and ("triggerList" in path or "untriggeredList" in path):
                            return {"code": 0, "msg": "", "data": {"rows": []}}
                        raise RuntimeError(f"HTTP {resp.status}: {text}")
                    data = json.loads(text)
                    code = int(data.get("code", 0))
                    if code == 10002: # OM_ORDER_NOT_FOUND means empty list
                        return {"code": 0, "msg": "", "data": {"rows": []}}
                    if code != 0:
                        raise RuntimeError(f"Phemex Error [{code}]: {data.get('msg', '')}")
                    return data
            except Exception as e:
                last_err = e
                if attempt < attempts: await asyncio.sleep(0.5 * attempt)
        
        logger.error(f"API Request Failed ({method} {path}): {last_err}")
        raise RuntimeError(f"Private API request failed: {last_err}")

    async def get_active_positions(self, currency: str = "USDT") -> Dict[str, Any]:
        return await self._request("GET", "/g-accounts/accountPositions", query_no_q=f"currency={currency}")

    async def get_active_symbols(self) -> Set[str]:
        """
        Получает список символов с активными открытыми позициями.
        """
        active_symbols = set()
        try:
            res = await self.get_active_positions("USDT")
            if res and res.get("code") == 0:
                positions = res.get("data", {}).get("positions", [])
                for p in positions:
                    side = p.get("side", "None")
                    size = abs(float(p.get("size", 0) or p.get("sizeRv", 0) or 0))
                    if side != "None" and size > 0:
                        symbol = p.get("symbol")
                        if symbol:
                            active_symbols.add(symbol.upper())
        except Exception as e:
            logger.warning(f"Failed to fetch active positions: {e}")
            
        return active_symbols


if __name__ == "__main__":
    import os
    import sys
    from dotenv import load_dotenv
    from pathlib import Path
    
    # Add workspace directory to python path
    base_dir = Path(__file__).resolve().parent.parent.parent
    if str(base_dir) not in sys.path:
        sys.path.append(str(base_dir))
        
    async def main():
        load_dotenv(dotenv_path=base_dir / ".env")
        api_key = os.getenv("PHEMEX_API_KEY")
        api_secret = os.getenv("PHEMEX_API_SECRET")
        
        if not api_key or not api_secret:
            print("PHEMEX_API_KEY and PHEMEX_API_SECRET must be set in .env")
            return
            
        async with aiohttp.ClientSession() as session:
            client = PhemexPrivateRESTFallback(api_key, api_secret, session)
            
            active_symbols = await client.get_active_symbols()
            print("Активные символы:", list(active_symbols))

    asyncio.run(main())
