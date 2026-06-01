# ============================================================
# FILE: API/TG/notifier.py
# ROLE: Telegram нотификатор и менеджер агрегации всех каналов уведомлений.
# ============================================================

from typing import Optional, List, Any
from c_log import UnifiedLogger
from API.pushover.base import NotifierBase

logger = UnifiedLogger("notifier", spam_throttle=1.0)

import time
import asyncio

class TelegramNotifier(NotifierBase):
    platform = "telegram"
    def __init__(self, bot: Any, allowed_user_ids: List[int], enabled: bool = True) -> None:
        self.bot = bot
        self.user_ids = allowed_user_ids
        self._enabled = enabled and bool(self.bot) and bool(self.user_ids)
        self._lock = asyncio.Lock()
        self._last_send_time = 0.0

    def _get_burst_delay(self) -> float:
        try:
            from consts import _store
            return _store.config.app.telegram_burst_delay_sec
        except Exception:
            return 1.0

    @property
    def enabled(self) -> bool:
        try:
            from consts import _store
            return _store.config.telegram_alerts.enabled and bool(self.bot) and bool(self.user_ids)
        except Exception:
            return self._enabled

    async def send(self, title: str, body: str, sound: Optional[str] = None, **kwargs) -> bool:
        if not self.enabled:
            return False

        # В тестовом режиме мы все равно шлем сообщения в Telegram (т.к. он включен в alerts.json)
        text = f"<b>{title}</b>\n\n{body}"
        results = []
        
        async with self._lock:
            now = time.monotonic()
            delay = self._get_burst_delay()
            elapsed = now - self._last_send_time
            if elapsed < delay:
                sleep_time = delay - elapsed
                logger.info("⏳ Защита от спама Telegram: ожидание %.2f сек перед следующей отправкой...", sleep_time)
                await asyncio.sleep(sleep_time)
            
            for user_id in self.user_ids:
                try:
                    await self.bot.send_message(user_id, text)
                    results.append(True)
                except Exception as e:
                    logger.error("Telegram notify error for %s: %s", user_id, e)
                    results.append(False)
            
            self._last_send_time = time.monotonic()
            
        return any(results)

class NotificationManager:
    def __init__(self, notifiers: List[NotifierBase]) -> None:
        self.notifiers = notifiers

    @property
    def enabled(self) -> bool:
        return any(n.enabled for n in self.notifiers)

    def _is_critical(self, title: str, priority: int) -> bool:
        # Для детектора делистингов все алерты критичны и должны доставляться на все каналы
        return True

    async def send(self, title: str, body: str, sound: Optional[str] = None, platforms: Optional[List[str]] = None, priority: int = 1, **kwargs) -> bool:
        # Тестового режима больше нет.

        if not self.enabled:
            return False
        
        is_crit = self._is_critical(title, priority)
        results = []
        for notifier in self.notifiers:
            if not notifier.enabled:
                continue
            if platforms and notifier.platform not in platforms:
                continue
            
            # Если пуш-платформы включены, шлем на них
            if notifier.platform in ("android", "apple", "apple2") and not is_crit:
                logger.info("ℹ️ Пропуск пуш-уведомления (%s) для некритичного события: %s", notifier.platform, title)
                continue
                
            res = await notifier.send(title, body, sound=sound, priority=priority, **kwargs)
            results.append(res)
        
        return any(results)

if __name__ == "__main__":
    import asyncio
    import os
    from dotenv import load_dotenv
    from consts import ConfigStore
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    async def test():
        env_file = ".env.test"
        config_file = "CONFIG/test"
        
        load_dotenv(dotenv_path=env_file, override=True)
        config_store = ConfigStore(config_file)
        
        logger_test = UnifiedLogger("telegram_notifier_test")
        logger_test.info("🚀 ТЕСТОВЫЙ ЗАПУСК: TELEGRAM NOTIFIER")
        logger_test.info("Loaded config from %s / %s", config_file, env_file)
        
        bot_token = os.getenv("TG_BOT_TOKEN", "")
        allowed_users = config_store.config.raw.get("telegram", {}).get("allowed_user_ids", [])
        enabled = config_store.config.raw.get("telegram", {}).get("enabled", False)
        
        logger_test.info("Bot Token: %s...", bot_token[:8] if bot_token else "None")
        logger_test.info("Allowed User IDs: %s", allowed_users)
        logger_test.info("Enabled in config: %s", enabled)
        
        confirm = input("Отправить тестовое сообщение в Telegram? (y/n): ")
        if confirm.lower() != 'y':
            logger_test.warning("Отменено.")
            return

        if not bot_token:
            logger_test.error("TG_BOT_TOKEN не задан!")
            return

        bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        notifier = TelegramNotifier(bot, allowed_users, enabled=enabled)
        
        if not notifier.enabled and allowed_users:
            logger_test.warning("Notifier disabled in config, forcing enabled for test.")
            notifier._enabled = True
            
        try:
            res = await notifier.send("Test Title", "This is a test notification from TelegramNotifier")
            logger_test.info("Результат отправки: %s", res)
        finally:
            await bot.session.close()

    asyncio.run(test())
