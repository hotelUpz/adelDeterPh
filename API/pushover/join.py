import aiohttp
from typing import Optional
from c_log import UnifiedLogger
from API.pushover.base import NotifierBase

logger = UnifiedLogger("join", spam_throttle=1.0)

import time
import asyncio

class JoinNotifier(NotifierBase):
    platform = "android"

    def __init__(self, session: aiohttp.ClientSession, api_key: str, device_id: str = "group.all", enabled: bool = True) -> None:
        self.session = session
        self.api_key = api_key
        self.device_id = device_id
        self.enabled = enabled

    async def send(self, title: str, body: str, sound: Optional[str] = None, **kwargs) -> bool:
        if not self.enabled:
            return False

        if not self.api_key:
            logger.warning("Отправка пуша отменена: не задан API ключ (Join)")
            return False

        url = "https://joinjoaomgcd.appspot.com/_ah/api/messaging/v1/sendPush"
        params = {
            "apikey": self.api_key,
            "deviceId": self.device_id,
            "title": title,
            "text": body,
            "alarmVolume": "75",      
            "ringVolume": "75",  
            "mediaVolume": "75",    
            "category": "my_alarm",
            "timeout": "10000"
        }

        try:
            async with self.session.get(url, params=params) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("success"):
                        logger.debug("Join push sent: %s", title)
                        return True
                    else:
                        logger.error("Join API Error: %s", result.get("errorMessage"))
                else:
                    logger.error("Join HTTP Error: %s", response.status)
                return False
        except Exception as e:
            logger.error("Join Connection Error: %s", e)
            return False