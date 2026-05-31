import aiohttp
from typing import Optional
from c_log import UnifiedLogger
from API.pushover.base import NotifierBase

logger = UnifiedLogger("alertzy", spam_throttle=1.0)

import time
import asyncio

class AlertzyNotifier(NotifierBase):
    platform = "apple2"
    def __init__(self, session: aiohttp.ClientSession, account_key: str, enabled: bool = True) -> None:
        self.session = session
        self.account_key = account_key.strip()
        self._enabled = enabled and bool(self.account_key)
        self.url = "https://alertzy.app/send"
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
            return _store.config.notifier_apple2.enabled and bool(self.account_key)
        except Exception:
            return self._enabled

    async def send(self, title: str, body: str, sound: Optional[str] = None, **kwargs) -> bool:
        if not self.enabled:
            return False

        try:
            from consts import _store
            if _store.config.app.test_mode:
                logger.info("🚫 [TEST MODE] Alertzy notify bypassed (logging only):\nTitle: %s\nBody: %s", title, body)
                return True
        except Exception:
            pass

        payload = {
            "accountKey": self.account_key,
            "title": title,
            "message": body,
        }

        async with self._lock:
            now = time.monotonic()
            delay = self._get_burst_delay()
            elapsed = now - self._last_send_time
            if elapsed < delay:
                sleep_time = delay - elapsed
                logger.info("⏳ Защита от спама Alertzy (Apple2): ожидание %.2f сек...", sleep_time)
                await asyncio.sleep(sleep_time)

            try:
                async with self.session.post(self.url, data=payload, timeout=10) as resp:
                    if resp.status == 200:
                        logger.info("Alertzy Push sent: %s", title)
                        return True
                    text = await resp.text()
                    logger.error("Alertzy Push error %s: %s", resp.status, text)
                    return False
            except Exception as e:
                logger.error("Alertzy Push exception: %s", e)
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
        
        logger_test = UnifiedLogger("alertzy_test")
        logger_test.info("🚀 ТЕСТОВЫЙ ЗАПУСК: ALERTZY PUSH (APPLE2)")
        logger_test.info("Loaded config from %s / %s", config_file, env_file)
        
        account_key = os.getenv("APPLE2_ALERTZY_KEY", "")
        enabled = config_store.config.raw.get("notifier_apple2", {}).get("enabled", False)
        
        logger_test.info("Account Key: %s...", account_key[:8] if account_key else "None")
        logger_test.info("Enabled in config: %s", enabled)
        
        confirm = input("Отправить тестовый пуш? (y/n): ")
        if confirm.lower() != 'y':
            logger_test.warning("Отменено.")
            return

        async with aiohttp.ClientSession() as session:
            notifier = AlertzyNotifier(session, account_key=account_key, enabled=enabled)
            
            if not notifier.enabled and account_key:
                logger_test.warning("Notifier disabled in config, forcing enabled for test.")
                notifier._enabled = True
                
            res = await notifier.send("Test Title", "This is a test notification from Alertzy (Apple2)")
            logger_test.info("Результат отправки: %s", res)

    asyncio.run(test())
