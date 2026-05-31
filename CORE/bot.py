# File: CORE/bot.py
# Role: Application entry point and orchestrator.
#       Владеет двумя фоновыми циклами:
#       1. _delisting_poll_loop  (delisting_poll_interval_sec) — опрос биржи на делистинги
#       2. _game_loop            (game_loop_interval_sec)      — сверка снапшотов и алерты
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, Set

from dotenv import load_dotenv
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import consts
from consts import ConfigStore
from c_log import UnifiedLogger
from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.private_rest_fallback import PhemexPrivateRESTFallback
from API.PHEMEX.ws_private import PhemexPrivateWS
from API.pushover.pushover import PushoverNotifier
from API.pushover.techulus import TechulusPushNotifier
from API.pushover.alertzy import AlertzyNotifier
from API.TG.notifier import TelegramNotifier, NotificationManager
from CORE.detector import DelistingDetector

logger = UnifiedLogger("bot", spam_throttle=1.0)


class RuntimeContext:
    def __init__(self):
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self.config_store: ConfigStore | None = None
        self.detector: DelistingDetector | None = None
        self.notifier_manager: NotificationManager | None = None
        self.private_client: PhemexPrivateRESTFallback | None = None
        self.symbols_api: PhemexSymbols | None = None
        self.ws_client: PhemexPrivateWS | None = None
        self.session: aiohttp.ClientSession | None = None

        # Background tasks
        self.ws_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._game_task: asyncio.Task | None = None

        # Shared state (written by loops, read by game loop)
        self.is_monitoring: bool = False
        self.delisted_symbols: Set[str] = set()
        self.active_alerts: Dict[str, Dict[str, Any]] = {}  # symbol -> {task}


ctx = RuntimeContext()


# ─────────────────────────────────────────────
# Application bootstrap
# ─────────────────────────────────────────────
async def start_app():
    # 1. Load CONFIG/prod/app.json to check test_mode flag
    prod_config_dir = Path("CONFIG/prod")
    test_mode = True
    app_json_path = prod_config_dir / "app.json"
    if app_json_path.exists():
        try:
            with open(app_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                test_mode = bool(data.get("app", {}).get("test_mode", True))
        except Exception as e:
            logger.error("Failed to read test_mode from prod app.json, defaulting to True. Error: %s", e)

    # 2. Set up environment and config store directory based on test_mode
    if test_mode:
        env_file = ".env.test"
        config_dir = "CONFIG/test"
        logger.info("🔧 Режим: [TEST/SANDBOX]. Загружаем %s и конфиги из %s", env_file, config_dir)
    else:
        env_file = ".env"
        config_dir = "CONFIG/prod"
        logger.info("🔥 Режим: [PRODUCTION]. Загружаем %s и конфиги из %s", env_file, config_dir)

    load_dotenv(dotenv_path=env_file, override=True)

    # Initialize ConfigStore
    ctx.config_store = ConfigStore(config_dir)
    consts._store = ctx.config_store

    # 3. Read env vars
    tg_token = os.getenv("TG_BOT_TOKEN", "").strip()
    phemex_key = os.getenv("PHEMEX_API_KEY", "").strip()
    phemex_secret = os.getenv("PHEMEX_API_SECRET", "").strip()
    pushover_token = os.getenv("ANDROID_PUSHOVER_TOKEN", "").strip()
    pushover_user = os.getenv("ANDROID_PUSHOVER_USER", "").strip()
    apple_key = os.getenv("APPLE_NOTIFIER_KEY", "").strip()
    apple2_key = os.getenv("APPLE2_ALERTZY_KEY", "").strip()

    if not tg_token:
        logger.error("❌ TG_BOT_TOKEN не задан! Бот не может быть запущен.")
        return

    # 4. Initialize HTTP session
    ctx.session = aiohttp.ClientSession()

    # 5. Initialize Phemex Clients
    ctx.symbols_api = PhemexSymbols()
    ctx.private_client = PhemexPrivateRESTFallback(phemex_key, phemex_secret, ctx.session)

    # 6. Initialize Bot and Dispatcher
    ctx.bot = Bot(token=tg_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    ctx.dp = Dispatcher()

    # Register handlers from telegram_bot
    from API.TG.telegram_bot import register_handlers
    register_handlers(ctx.dp)

    # 7. Initialize Notifiers
    allowed_users = ctx.config_store.config.telegram.allowed_user_ids
    tg_enabled = ctx.config_store.config.telegram_alerts.enabled

    tg_notifier = TelegramNotifier(ctx.bot, allowed_users, enabled=tg_enabled)

    pushover_enabled = ctx.config_store.config.notifier_android.enabled
    pushover_notifier = PushoverNotifier(ctx.session, token=pushover_token, user=pushover_user, enabled=pushover_enabled)

    techulus_enabled = ctx.config_store.config.notifier_apple.enabled
    techulus_notifier = TechulusPushNotifier(ctx.session, api_key=apple_key, enabled=techulus_enabled)

    alertzy_enabled = ctx.config_store.config.notifier_apple2.enabled
    alertzy_notifier = AlertzyNotifier(ctx.session, account_key=apple2_key, enabled=alertzy_enabled)

    ctx.notifier_manager = NotificationManager([
        tg_notifier,
        pushover_notifier,
        techulus_notifier,
        alertzy_notifier
    ])

    # 8. Initialize Detector (pure WS interpreter, no loops)
    ctx.detector = DelistingDetector()

    # 9. Start Telegram Bot polling
    logger.info("🤖 Telegram бот запускает polling...")
    try:
        await ctx.dp.start_polling(ctx.bot)
    finally:
        # Cleanup
        logger.info("🧹 Завершение работы приложения, очистка ресурсов...")
        await stop_monitoring_services()
        await ctx.symbols_api.aclose()
        await ctx.session.close()
        await ctx.bot.session.close()


# ─────────────────────────────────────────────
# Start / Stop monitoring
# ─────────────────────────────────────────────
async def start_monitoring_services() -> bool:
    if ctx.is_monitoring:
        return False

    ctx.is_monitoring = True

    # 1. Start WS connection (фоновый интерпретатор позиций)
    phemex_key = os.getenv("PHEMEX_API_KEY", "").strip()
    phemex_secret = os.getenv("PHEMEX_API_SECRET", "").strip()
    if phemex_key and phemex_secret:
        ctx.ws_client = PhemexPrivateWS(phemex_key, phemex_secret)

        def on_ws_message(msg):
            ctx.detector.process_ws_message(msg)

        ctx.ws_task = asyncio.create_task(ctx.ws_client.run(on_ws_message))
        logger.info("🔑 Phemex API ключи обнаружены. Приватный WS запущен.")
    else:
        logger.warning("⚠️ Phemex API ключи не заданы. WS отключен.")

    # 2. Start delisting poll loop
    ctx._poll_task = asyncio.create_task(_delisting_poll_loop())

    # 3. Start game loop
    ctx._game_task = asyncio.create_task(_game_loop())

    logger.info("🚀 Мониторинг делистингов запущен.")
    return True


async def stop_monitoring_services() -> bool:
    if not ctx.is_monitoring:
        return False

    ctx.is_monitoring = False

    # 1. Stop poll loop
    if ctx._poll_task:
        ctx._poll_task.cancel()
        ctx._poll_task = None

    # 2. Stop game loop
    if ctx._game_task:
        ctx._game_task.cancel()
        ctx._game_task = None

    # 3. Cancel all active repeating alerts
    for item in list(ctx.active_alerts.values()):
        item["task"].cancel()
    ctx.active_alerts.clear()

    # 4. Stop WS client
    if ctx.ws_client:
        await ctx.ws_client.aclose()
        ctx.ws_client = None
    if ctx.ws_task:
        ctx.ws_task.cancel()
        ctx.ws_task = None

    logger.info("🛑 Приватный WS и мониторинг остановлены.")
    return True


# ─────────────────────────────────────────────
# Loop 1: Delisting Poll (опрос биржи на делистинги)
# ─────────────────────────────────────────────
async def _delisting_poll_loop():
    """Периодический REST-опрос публичных продуктов биржи на предмет делистинга."""
    while ctx.is_monitoring:
        try:
            symbols = await ctx.symbols_api.get_all(only_active=False)
            new_delisted = set()
            for sym in symbols:
                if str(sym.status or "").strip() == "Delisted":
                    new_delisted.add(sym.symbol)
            ctx.delisted_symbols = new_delisted
            logger.info("Обновлен список делистингов биржи: %d монет(ы)", len(ctx.delisted_symbols))
        except Exception as e:
            logger.error("Error in delisting poll loop: %s", e)

        await asyncio.sleep(ctx.config_store.config.app.delisting_poll_interval_sec)


# ─────────────────────────────────────────────
# Loop 2: Game Loop (сверка снапшотов + алерты)
# ─────────────────────────────────────────────
async def _game_loop():
    """Основной игровой цикл. Читает снапшоты delisted_symbols и detector.active_symbols,
    проверяет пересечения, стреляет повторяющиеся алерты."""
    while ctx.is_monitoring:
        try:
            # Снапшот активных символов из WS-интерпретатора
            active = ctx.detector.active_symbols if ctx.detector else set()
            # Снапшот делистнутых символов из poll loop
            delisted = ctx.delisted_symbols

            current_overlap = active.intersection(delisted)

            # Отменяем алерты для монет, которых больше нет в пересечении
            for symbol in list(ctx.active_alerts.keys()):
                if symbol not in current_overlap:
                    ctx.active_alerts[symbol]["task"].cancel()
                    del ctx.active_alerts[symbol]
                    logger.info("ℹ️ Монета %s ушла из пересечения. Алерт сброшен.", symbol)

            # Запускаем повторяющиеся алерты для новых монет в пересечении
            for symbol in current_overlap:
                if symbol not in ctx.active_alerts:
                    task = asyncio.create_task(_send_repeating_alert(symbol))
                    ctx.active_alerts[symbol] = {"symbol": symbol, "task": task}
                    logger.warning("🚨 Найдено перекрестное совпадение делистинга с активным ордером/позицией: %s", symbol)

        except Exception as e:
            logger.error("Error in game loop: %s", e)

        await asyncio.sleep(ctx.config_store.config.app.game_loop_interval_sec)


# ─────────────────────────────────────────────
# Alert sender (repeating)
# ─────────────────────────────────────────────
async def _send_repeating_alert(symbol: str):
    """Отправляет повторяющийся алерт для символа с пересечением делистинга и активной позиции."""
    cfg = ctx.config_store.config
    repeats = cfg.delisting_repeats
    interval = cfg.delisting_interval_sec
    title_template = cfg.alert_title_template
    body_template = cfg.alert_body_template

    for i in range(repeats):
        if not ctx.is_monitoring:
            break

        title = title_template.format(symbol=symbol, symbols=symbol)
        body = body_template.format(
            symbol=symbol,
            symbols=symbol,
            repeat_num=i + 1,
            total_repeats=repeats
        )

        try:
            await ctx.notifier_manager.send(
                title=title,
                body=body,
                priority=1
            )
        except Exception as e:
            logger.error("Failed to send delisting alert for %s: %s", symbol, e)

        if i < repeats - 1:
            await asyncio.sleep(interval)
