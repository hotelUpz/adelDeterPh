# ============================================================
# FILE: API/pushover/base.py
# ROLE: Базовый интерфейс для Push-уведомителей.
# ============================================================

from typing import Optional

class NotifierBase:
    platform: str = "base"
    enabled: bool = False
    async def send(self, title: str, body: str, sound: Optional[str] = None, **kwargs) -> bool:
        raise NotImplementedError
