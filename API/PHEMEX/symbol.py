# ============================================================
# FILE: API/PHEMEX/symbol.py
# ROLE: Phemex USDT Perpetual (Futures) symbols via REST (Plus Version).
# python -m API.PHEMEX.symbol
# ============================================================

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    status: str
    quote: str
    tick_size: Optional[float]
    lot_size: Optional[float]
    max_leverage: Optional[float]
    delist_time: Optional[int] = None  # Таймстамп делистинга в мс (из timeline[3])


class PhemexSymbols:
    def __init__(self, test_mode: bool = False, timeout_sec: float = 20.0, retries: int = 3):
        self._timeout = aiohttp.ClientTimeout(total=float(timeout_sec))
        self._retries = int(retries)
        self.BASE_URL = "https://testnet-api.phemex.com" if test_mode else "https://api.phemex.com"
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                return self._session
            connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(timeout=self._timeout, connector=connector)
            return self._session

    async def aclose(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    async def _get_json(self, path: str) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        last_err: Optional[Exception] = None
        for attempt in range(1, self._retries + 1):
            try:
                session = await self._get_session()
                async with session.get(url) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}: {text}")
                    data = await resp.json()
                    if not isinstance(data, dict):
                        raise RuntimeError(f"Bad JSON root: {type(data)}")
                    return data
            except Exception as e:
                last_err = e
                s = (str(e) or "").lower()
                if "session is closed" in s or "connector is closed" in s or "clientconnectorerror" in s:
                    self._session = None
                if attempt < self._retries:
                    await asyncio.sleep(0.4 * attempt)
                else:
                    break
        raise RuntimeError(f"Phemex symbols failed: {path} err={last_err}")

    @staticmethod
    def _norm_quote(v: Any) -> str:
        return (str(v) if v is not None else "").upper().strip()

    @staticmethod
    def _to_float(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _is_active_status(status: str) -> bool:
        s = str(status or "").strip().lower()
        if not s:
            return True
        banned = ("delist", "suspend", "pause", "settle", "close", "expired")
        return not any(word in s for word in banned)

    def _parse_perp(self, obj: Dict[str, Any], quote: str = "USDT") -> Optional[SymbolInfo]:
        sym = obj.get("symbol")
        if not sym:
            return None

        q = self._norm_quote(obj.get("quoteCurrency") or obj.get("settleCurrency") or "")
        if q != self._norm_quote(quote):
            return None

        sym_s = str(sym).strip()
        if sym_s.startswith("s"):
            return None

        status = str(obj.get("status") or obj.get("state") or obj.get("symbolStatus") or "Listed")

        tick_size = self._to_float(obj.get("tickSize"))
        lot_size = self._to_float(obj.get("qtyStepSize"))
        max_lvg = self._to_float(obj.get("limitOrderMaxLeverage") or obj.get("maxLeverage"), 20)

        # Вытаскиваем делистинг таймстамп из timeline[3]
        delist_timestamp = None
        timeline = obj.get("timeline")
        if isinstance(timeline, list) and len(timeline) > 3:
            try:
                val = int(timeline[3])
                if val > 0:
                    delist_timestamp = val
            except (ValueError, TypeError):
                pass

        return SymbolInfo(
            symbol=sym_s.upper(),
            status=status,
            quote=q,
            tick_size=tick_size,
            lot_size=lot_size,
            max_leverage=max_lvg,
            delist_time=delist_timestamp
        )

    async def get_all(self, quote: str = "USDT") -> List[SymbolInfo]:
        data = await self._get_json("/public/products-plus")
        root = data.get("data") if isinstance(data, dict) else None
        if not isinstance(root, dict): return []

        arr = root.get("perpProductsV2") or root.get("perpProducts") or []
        out: List[SymbolInfo] = []
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict):
                    si = self._parse_perp(it, quote=quote)
                    if si:
                        out.append(si)

        seen = set()
        uniq: List[SymbolInfo] = []
        for s in out:
            if s.symbol not in seen:
                seen.add(s.symbol)
                uniq.append(s)
        return uniq

    async def get_asoon_delisting_symbols(self, quote: str = "USDT") -> List[SymbolInfo]:
        """
        Возвращает отфильтрованный список монет, у которых запланирован скорый делистинг
        (таймстамп из timeline[3] больше текущего системного времени).
        """
        rows = await self.get_all(quote=quote)
        now_ms = int(time.time() * 1000)
        return [r for r in rows if r.delist_time and r.delist_time > now_ms]


# # ------------------------------------------------------------
# # БЛОК ТЕСТИРОВАНИЯ: СКОРЫЕ ДЕЛИСТИНГИ (через целевой метод)
# # ------------------------------------------------------------
if __name__ == "__main__":
    from datetime import datetime, timezone

    async def _main():
        # Поставь True, если нужно проверить тестнет
        api = PhemexSymbols(test_mode=False) 
        try:
            print("⏳ Запрашиваем скорые делистинги через get_asoon_delisting_symbols()...")
            
            # Тестируем целевой метод внутри класса
            upcoming_delistings = await api.get_asoon_delisting_symbols(quote="USDT")
            now_ms = int(time.time() * 1000)
            
            print(f"Монет со скорым делистингом обнаружено: {len(upcoming_delistings)}\n")
            
            if not upcoming_delistings:
                print("✅ На данный момент в API нет запланированных будущих делистингов.")
            else:
                print("=" * 95)
                print(f"{'СИМВОЛ':<14} | {'СТАТУС':<10} | {'ТОЧНАЯ ДАТА ДЕЛИСТИНГА (UTC)':<26} | {'ОСТАЛОСЬ ДО ВЫЛЕТА'}")
                print("=" * 95)
                
                for r in upcoming_delistings:
                    # Вычисляем разницу во времени
                    diff_ms = r.delist_time - now_ms
                    diff_sec = diff_ms // 1000
                    
                    # Переводим в дни, часы, минуты и секунды
                    days = diff_sec // 86400
                    hours = (diff_sec % 86400) // 3600
                    minutes = (diff_sec % 3600) // 60
                    seconds = diff_sec % 60
                    
                    time_left_str = f"{days}д {hours}ч {minutes}м {seconds}с"
                    
                    # Превращаем таймстамп миллисекунд в точную дату UTC
                    exact_date = datetime.fromtimestamp(r.delist_time / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                    
                    print(f"{r.symbol:<14} | {r.status:<10} | {exact_date:<26} | {time_left_str}")
                print("=" * 95)
                
        except Exception as e:
            print(f"❌ Ошибка во время выполнения теста: {e}")
        finally:
            await api.aclose()

    asyncio.run(_main())