# ============================================================
# FILE: CORE/bot.py
# ROLE: Главный оркестратор — шейм-лупа, кэш в памяти, запуск TG-бота.
# ============================================================

import asyncio
import os
import time
import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import consts
from c_log import UnifiedLogger

# Прямой импорт оригинальных классов биржи напрямую, без оберток
from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.get_pos_symbols import PhemexPrivateRESTFallback

# Импорт нотификаторов
from API.TG.notifier import TelegramNotifier, NotificationManager
from API.TG.telegram_bot import register_handlers
from API.pushover.pushover import PushoverNotifier
from API.pushover.techulus import TechulusPushNotifier
from API.pushover.alertzy import AlertzyNotifier

logger = UnifiedLogger("orchestrator")

# --- ДИНАМИЧЕСКИЙ ПАТЧИНГ ЛЕГАСИ-МЕТОДОВ ДЛЯ СОВМЕСТИМОСТИ С telegram_bot.py ---
# Избавляемся от создания мусорных классов (Wrapper/Extended), сохраняя чистоту ядра.
_orig_get_all = PhemexSymbols.get_all
async def _patched_get_all(self, quote: str = "USDT", *args, **kwargs):
    return await _orig_get_all(self, quote=quote)
PhemexSymbols.get_all = _patched_get_all

async def _dummy_legacy_orders(self, *args, **kwargs):
    return {"code": 0, "msg": "Legacy stub", "data": {"rows": []}}
PhemexPrivateRESTFallback.get_active_orders = _dummy_legacy_orders
PhemexPrivateRESTFallback.get_conditional_orders = _dummy_legacy_orders


class DetectorState:
    """Вспомогательный контейнер для хранения структуры active_symbols."""
    def __init__(self):
        self.active_symbols = set()


class BotContext:
    """Единый контекст приложения (state), разделяемый с UI-обработчиками бота."""
    def __init__(self):
        self.config_store = None
        self.is_monitoring = False
        self.delisted_symbols = set()  # Кэш делистингов в памяти (названия монет в UPPERCASE)
        self.detector = DetectorState()
        self.active_alerts = {}        # Кэш сигналов в оперативной памяти: {SYMBOL: {"repeat_num": int, "last_sent": float}}
        
        self.symbols_api = None
        self.private_client = None
        self.notifier_manager = None
        self.bot = None
        
        self._loop_task = None


# Глобальный синглтон контекста, который импортирует telegram_bot.py
ctx = BotContext()


async def update_delisted_cache():
    """Обновление черного списка делистинговых монет из публичного API."""
    try:
        symbols = await ctx.symbols_api.get_all()
        new_delisted = set()
        for sym in symbols:
            status_str = str(sym.status or "").strip().lower()
            # Если статус не активен или содержит упоминание делистинга — заносим в черный список
            if not ctx.symbols_api._is_active_status(sym.status) or "delist" in status_str:
                new_delisted.add(sym.symbol.upper())
        ctx.delisted_symbols = new_delisted
        logger.info("Кэш делистинговых монет обновлен. Найдено инструментов: %d", len(ctx.delisted_symbols))
    except Exception as e:
        logger.error("Ошибка при обновлении кэша делистингов: %s", e)


async def _shame_loop():
    """Основная шейм-лупа (гейм-лупа) непрерывного мониторинга открытых позиций."""
    last_cache_update = 0.0
    
    while ctx.is_monitoring:
        try:
            now = time.time()
            cfg = ctx.config_store.config
            
            # Обновляем карту делистингов биржи раз в 5 минут (300 секунд)
            if now - last_cache_update > 300.0:
                await update_delisted_cache()
                last_cache_update = now
            
            # Получаем текущие активные символы с открытыми позициями
            active_symbols = await ctx.private_client.get_active_symbols()
            ctx.detector.active_symbols = active_symbols
            
            # Находим пересечение: активные позиции по делистинговым монетам
            matches = active_symbols.intersection(ctx.delisted_symbols)
            
            # Потокобезопасно вычищаем из памяти алерты по закрытым позициям
            for sym in list(ctx.active_alerts.keys()):
                if sym not in active_symbols:
                    logger.info("Позиция по %s закрыта на бирже. Удаляем сигнал из памяти.", sym)
                    del ctx.active_alerts[sym]
            
            # Обработка текущих сигналов делистинга
            for sym in matches:
                if sym not in ctx.active_alerts:
                    # Новый сигнал: пишем в память инстанса и отправляем мгновенно
                    ctx.active_alerts[sym] = {"repeat_num": 1, "last_sent": now}
                    await _send_alert_notification(sym, 1, cfg.delisting_repeats)
                else:
                    alert = ctx.active_alerts[sym]
                    if alert["repeat_num"] < cfg.delisting_repeats:
                        # Проверяем интервал для повторной отправки (строго по настройкам)
                        if now - alert["last_sent"] >= cfg.delisting_interval_sec:
                            alert["repeat_num"] += 1
                            alert["last_sent"] = now
                            await _send_alert_notification(sym, alert["repeat_num"], cfg.delisting_repeats)
                            
        except Exception as e:
            logger.error("Ошибка итерации в цикле мониторинга: %s", e)
            
        await asyncio.sleep(ctx.config_store.config.app.game_loop_interval_sec)


async def _send_alert_notification(symbol: str, repeat_num: int, total_repeats: int):
    """Рендеринг шаблонов уведомлений и отправка во все включенные пуши."""
    try:
        cfg = ctx.config_store.config
        title = cfg.alert_title_template.format(symbols=symbol)
        body = cfg.alert_body_template.format(symbols=symbol, repeat_num=repeat_num, total_repeats=total_repeats)
        
        await ctx.notifier_manager.send(title, body, priority=1)
        logger.warning("🚨 СИГНАЛ ДЕЛИСТИНГА: %s [Повтор %d/%d]", symbol, repeat_num, total_repeats)
    except Exception as e:
        logger.error("Не удалось отправить уведомление по сигналу %s: %s", symbol, e)


async def start_monitoring_services():
    """Включение фонового процесса сканирования."""
    if ctx.is_monitoring:
        return
    ctx.is_monitoring = True
    ctx._loop_task = asyncio.create_task(_shame_loop())
    logger.info("Фоновый мониторинг позиций запущен.")


async def stop_monitoring_services():
    """Остановка фонового процесса сканирования."""
    if not ctx.is_monitoring:
        return
    ctx.is_monitoring = False
    if ctx._loop_task:
        ctx._loop_task.cancel()
        try:
            await ctx._loop_task
        except asyncio.CancelledError:
            pass
        ctx._loop_task = None
    logger.info("Фоновый мониторинг позиций остановлен.")


async def start_app():
    """Точка входа. Инициализация клиентов, подключение нотификаторов и старт Telegram-сервера."""
    load_dotenv()
    
    ctx.config_store = consts._store
    cfg = ctx.config_store.config
    
    bot_token = os.getenv("TG_BOT_TOKEN", "")
    if not bot_token:
        logger.error("TG_BOT_TOKEN не задан в переменной окружения (.env)!")
        return
        
    # Настройка инстанса бота и диспетчера aiogram
    ctx.bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    register_handlers(dp)
    
    # Общая HTTP сессия для асинхронных запросов к API и пушам
    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, enable_cleanup_closed=True)
    session = aiohttp.ClientSession(connector=connector)
    
    # Инициализация оригинальных клиентов Phemex напрямую
    ctx.symbols_api = PhemexSymbols(test_mode=cfg.app.test_mode)
    ctx.private_client = PhemexPrivateRESTFallback(
        api_key=os.getenv("PHEMEX_API_KEY", ""),
        api_secret=os.getenv("PHEMEX_API_SECRET", ""),
        session=session
    )
    
    # Сборка пуш-провайдеров в СТРОГОМ соответствии с индексами из telegram_bot.py:
    # [0] -> Telegram, [1] -> Pushover, [2] -> Techulus, [3] -> Alertzy
    tg_notifier = TelegramNotifier(ctx.bot, cfg.telegram.allowed_user_ids, enabled=cfg.telegram_alerts.enabled)
    pushover_notifier = PushoverNotifier(session, os.getenv("ANDROID_PUSHOVER_TOKEN", ""), os.getenv("ANDROID_PUSHOVER_USER", ""), enabled=cfg.notifier_android.enabled)
    techulus_notifier = TechulusPushNotifier(session, os.getenv("APPLE_NOTIFIER_KEY", ""), enabled=cfg.notifier_apple.enabled)
    alertzy_notifier = AlertzyNotifier(session, os.getenv("APPLE2_ALERTZY_KEY", ""), enabled=cfg.notifier_apple2.enabled)
    
    ctx.notifier_manager = NotificationManager([
        tg_notifier,
        pushover_notifier,
        techulus_notifier,
        alertzy_notifier
    ])
    
    # Первичное наполнение кэша перед стартом лупы
    await update_delisted_cache()
    
    # Автоматический старт шейм-лупы при запуске приложения
    await start_monitoring_services()
    
    logger.info("Запуск aiogram polling...")
    try:
        await dp.start_polling(ctx.bot)
    finally:
        await stop_monitoring_services()
        await session.close()
        await ctx.symbols_api.aclose()
        await ctx.bot.session.close()