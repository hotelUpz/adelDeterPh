# ============================================================
# FILE: main.py
# ROLE: Точка входа (Entry point). Инициализация зависимостей и запуск приложения.
# ============================================================

import asyncio
import os
import sys
import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from consts import _store
from c_log import UnifiedLogger

from CORE.bot import DelistingOrchestrator
from API.TG.telegram_bot import register_handlers

# Импорт API биржи
from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.get_pos_symbols import PhemexPrivateRESTFallback
from API.GMAIL.gmail_monitor import GmailMonitor

# Импорт модулей нотификации
from API.TG.notifier import TelegramNotifier, NotificationManager
from API.pushover.pushover import PushoverNotifier
from API.pushover.techulus import TechulusPushNotifier
from API.pushover.alertzy import AlertzyNotifier
from API.pushover.join import JoinNotifier

logger = UnifiedLogger("main")

async def main():
    load_dotenv()
    logger.info("Запуск Delisting Detector Bot...")

    bot_token = os.getenv("TG_BOT_TOKEN", "")
    if not bot_token:
        logger.error("TG_BOT_TOKEN отсутствует в файле окружения (.env)! Завершение.")
        return

    # 1. Инициализация Telegram Бота и Роутера
    bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    register_handlers(dp)

    # 2. Пул соединений для всех HTTP запросов (Биржа + Push-серверы)
    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, enable_cleanup_closed=True)
    session = aiohttp.ClientSession(connector=connector)

    cfg = _store.config

    try:
        # 3. Инициализация API Phemex
        symbols_api = PhemexSymbols(test_mode=False)
        private_client = PhemexPrivateRESTFallback(
            api_key=os.getenv("PHEMEX_API_KEY", ""),
            api_secret=os.getenv("PHEMEX_API_SECRET", ""),
            session=session
        )

        # 4. Сборка массива нотификаторов строго по индексам из telegram_bot.py
        tg_notifier = TelegramNotifier(bot, cfg.telegram.allowed_user_ids, enabled=cfg.telegram_alerts.enabled)
        pushover_notifier = PushoverNotifier(session, os.getenv("ANDROID_PUSHOVER_TOKEN", ""), os.getenv("ANDROID_PUSHOVER_USER", ""), enabled=cfg.notifier_android.enabled)
        techulus_notifier = TechulusPushNotifier(session, os.getenv("APPLE_NOTIFIER_KEY", ""), enabled=cfg.notifier_apple.enabled)
        alertzy_notifier = AlertzyNotifier(session, os.getenv("APPLE2_ALERTZY_KEY", ""), enabled=cfg.notifier_apple2.enabled)
        join_notifier = JoinNotifier(session, os.getenv("ANDROID_JOIN_KEY", ""), enabled=cfg.notifier_join.enabled)

        notifier_manager = NotificationManager([
            tg_notifier,         # [0] Telegram
            pushover_notifier,   # [1] Pushover Android
            techulus_notifier,   # [2] Techulus iOS
            alertzy_notifier,    # [3] Alertzy iOS
            join_notifier        # [4] Join Android
        ])

        # 5. Инъекция собранных зависимостей в ядро оркестратора
        # Получаем полный список монет для парсера почты
        all_symbols_info = await symbols_api.get_all(quote="USDT")
        known_symbols = [s.symbol for s in all_symbols_info]
        gmail_monitor = GmailMonitor(all_known_symbols=known_symbols)
        
        ctx = DelistingOrchestrator()
        dp["ctx"] = ctx
        
        ctx.inject_dependencies(
            bot=bot,
            symbols_api=symbols_api,
            private_client=private_client,
            notifier_manager=notifier_manager,
            gmail_monitor=gmail_monitor
        )

        # 6. Запуск гейм-лупы и Telegram polling-сервера
        await ctx.start_monitoring()
        logger.info("Всё готово. Polling aiogram запущен.")
        await dp.start_polling(bot)

    except Exception as e:
        logger.critical(f"Критическая ошибка во время работы: {e}")
    finally:
        # 7. Корректное завершение и закрытие соединений
        logger.info("Остановка служб и закрытие сессий...")
        await ctx.stop_monitoring()
        await session.close()
        await symbols_api.aclose()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProcess terminated by user.")

# # chmod 600 ssh_key.txt
# # eval "$(ssh-agent -s)" 
# # ssh-add ssh_key.txt
# # source .ssh-autostart.sh
# # git push --set-upstream origin master
# # git config --global push.autoSetupRemote true
# # ssh -T git@github.com 
# # git log -1

# # git add .
# # git commit -m "plh37"
# # git push

# # pip install anthropic
# # npm install -g @anthropic-ai/claude-code

# # export ANTHROPIC_API_KEY=...
# # claude

# taskkill /F /IM python.exe