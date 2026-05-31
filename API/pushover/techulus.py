import aiohttp
from typing import Optional
from c_log import UnifiedLogger
from API.pushover.base import NotifierBase

logger = UnifiedLogger("techulus", spam_throttle=1.0)

import time
import asyncio

class TechulusPushNotifier(NotifierBase):
    platform = "apple"
    def __init__(self, session: aiohttp.ClientSession, api_key: str, enabled: bool = True) -> None:
        self.session = session
        self.api_key = api_key.strip()
        self._enabled = enabled and bool(self.api_key)
        self.url = "https://push.techulus.com/api/v1/notify"
        self._lock = asyncio.Lock()
        self._last_send_time = 0.0

    def _get_burst_delay(self) -> float:
        try:
            from consts import _store
            return _store.config.app.push_burst_delay_sec
        except Exception:
            return 30.0

    @property
    def enabled(self) -> bool:
        try:
            from consts import _store
            return _store.config.notifier_apple.enabled and bool(self.api_key)
        except Exception:
            return self._enabled

    async def send(self, title: str, body: str, sound: Optional[str] = None, **kwargs) -> bool:
        if not self.enabled:
            return False
        
        try:
            from consts import _store
            if _store.config.app.test_mode:
                logger.info("🚫 [TEST MODE] Techulus notify bypassed (logging only):\nTitle: %s\nBody: %s", title, body)
                return True
        except Exception:
            pass

        payload = {"title": title, "body": body}
        if sound:
            payload["sound"] = sound
        
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
        }

        async with self._lock:
            now = time.monotonic()
            delay = self._get_burst_delay()
            elapsed = now - self._last_send_time
            if elapsed < delay:
                sleep_time = delay - elapsed
                logger.info("⏳ Защита от спама Techulus (Apple): ожидание %.2f сек...", sleep_time)
                await asyncio.sleep(sleep_time)

            try:
                async with self.session.post(self.url, json=payload, headers=headers, timeout=10) as resp:
                    if resp.status == 200:
                        logger.info("Techulus Push sent: %s", title)
                        return True
                    text = await resp.text()
                    logger.error("Techulus Push error %s: %s", resp.status, text)
                    return False
            except Exception as e:
                logger.error("Techulus Push exception: %s", e)
                return False
            finally:
                self._last_send_time = time.monotonic()


if __name__ == "__main__":
    import asyncio
    import os
    import sys
    from dotenv import load_dotenv
    from consts import ConfigStore

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding='utf-8')

    async def test():
        env_file = ".env.test"
        config_file = "CONFIG/test"
        
        load_dotenv(dotenv_path=env_file, override=True)
        config_store = ConfigStore(config_file)
        
        logger_test = UnifiedLogger("techulus_test")
        logger_test.info("🚀 ТЕСТОВЫЙ ЗАПУСК: TECHULUS PUSH (APPLE)")
        logger_test.info("Loaded config from %s / %s", config_file, env_file)
        
        api_key = os.getenv("APPLE_NOTIFIER_KEY", "")
        enabled = config_store.config.raw.get("notifier_apple", {}).get("enabled", False)
        
        logger_test.info("API Key: %s...", api_key[:8] if api_key else "None")
        logger_test.info("Enabled in config: %s", enabled)
        
        confirm = input("Отправить тестовый пуш? (y/n): ")
        if confirm.lower() != 'y':
            logger_test.warning("Отменено.")
            return

        async with aiohttp.ClientSession() as session:
            notifier = TechulusPushNotifier(session, api_key=api_key, enabled=enabled)
            
            if not notifier.enabled and api_key:
                logger_test.warning("Notifier disabled in config, forcing enabled for test.")
                notifier._enabled = True
                
            res = await notifier.send("Test Title", "This is a test notification from Techulus (Apple)")
            logger_test.info("Результат отправки: %s", res)

    asyncio.run(test())
