# ============================================================
# FILE: API/pushover/pushover.py
# ROLE: Реализация уведомлений через Pushover (Android).
# ============================================================

import aiohttp
from typing import Optional
from c_log import UnifiedLogger
from API.pushover.base import NotifierBase

logger = UnifiedLogger("pushover", spam_throttle=1.0)

import time
import asyncio

class PushoverNotifier(NotifierBase):
    platform = "android"
    def __init__(self, session: aiohttp.ClientSession, token: str, user: str, enabled: bool = True) -> None:
        self.session = session
        self.token = token.strip()
        self.user = user.strip()
        self._enabled = enabled and bool(self.token) and bool(self.user)
        self.url = "https://api.pushover.net/1/messages.json"
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
            return _store.config.notifier_android.enabled and bool(self.token) and bool(self.user)
        except Exception:
            return self._enabled

    async def send(self, title: str, body: str, sound: Optional[str] = None, priority: int = 1, **kwargs) -> bool:
        if not self.enabled:
            return False

        # Тестового режима больше нет.

        # Map priority >= 1 to 1 for Pushover to avoid server-side repeating.
        # We control all alert repeats ourselves via our Python codebase.
        pushover_priority = 1 if priority >= 1 else priority
        data = {
            "token": self.token,
            "user": self.user,
            "message": body,
            "title": title,
            "priority": pushover_priority,
            "sound": sound or "alien"
        }

        async with self._lock:
            now = time.monotonic()
            delay = self._get_burst_delay()
            elapsed = now - self._last_send_time
            if elapsed < delay:
                sleep_time = delay - elapsed
                logger.info("⏳ Защита от спама Pushover (Android): ожидание %.2f сек...", sleep_time)
                await asyncio.sleep(sleep_time)

            try:
                async with self.session.post(self.url, data=data, timeout=10) as resp:
                    if resp.status == 200:
                        logger.info("Pushover Push sent: %s", title)
                        return True
                    text = await resp.text()
                    logger.error("Pushover Push error %s: %s", resp.status, text)
                    return False
            except Exception as e:
                logger.error("Pushover Push exception: %s", e)
                return False
            finally:
                self._last_send_time = time.monotonic()

if __name__ == "__main__":
    import asyncio
    import os
    from dotenv import load_dotenv
    from consts import ConfigStore

    async def test():
        env_file = ".env.test"
        config_file = "CONFIG/test"
        
        load_dotenv(dotenv_path=env_file, override=True)
        config_store = ConfigStore(config_file)
        
        logger_test = UnifiedLogger("pushover_test")
        logger_test.info("🚀 ТЕСТОВЫЙ ЗАПУСК: PUSHOVER PUSH (ANDROID)")
        logger_test.info("Loaded config from %s / %s", config_file, env_file)
        
        token = os.getenv("ANDROID_PUSHOVER_TOKEN", "")
        user = os.getenv("ANDROID_PUSHOVER_USER", "")
        enabled = config_store.config.raw.get("notifier_android", {}).get("enabled", False)
        
        logger_test.info("Token: %s...", token[:8] if token else "None")
        logger_test.info("User: %s...", user[:8] if user else "None")
        logger_test.info("Enabled in config: %s", enabled)
        
        confirm = input("Отправить тестовый пуш? (y/n): ")
        if confirm.lower() != 'y':
            logger_test.warning("Отменено.")
            return

        async with aiohttp.ClientSession() as session:
            notifier = PushoverNotifier(session, token=token, user=user, enabled=enabled)
            
            if not notifier.enabled and token and user:
                logger_test.warning("Notifier disabled in config, forcing enabled for test.")
                notifier._enabled = True
                
            res = await notifier.send("Test Title", "This is a test notification from Pushover (Android)")
            logger_test.info("Результат отправки: %s", res)

    asyncio.run(test())
