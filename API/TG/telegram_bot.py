# File: API/TG/telegram_bot.py
# Role: Telegram Bot UI Handlers.
from __future__ import annotations

import logging
from typing import Any
from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
import functools

from c_log import UnifiedLogger

logger = UnifiedLogger("tg_bot", spam_throttle=1.0)

_user_locks = {}

def idempotent_handler(func):
    @functools.wraps(func)
    async def wrapper(message: types.Message, *args, **kwargs):
        user_id = message.from_user.id
        if _user_locks.get(user_id):
            await message.answer("⏳ <i>Команда выполняется, пожалуйста подождите...</i>", parse_mode="HTML")
            return
        
        _user_locks[user_id] = True
        try:
            return await func(message, *args, **kwargs)
        finally:
            _user_locks[user_id] = False
    return wrapper

# Helper function to check if user is allowed
def is_allowed(event: types.Message | types.CallbackQuery) -> bool:
    from CORE.bot import ctx
    if not ctx.config_store:
        return False
    allowed = ctx.config_store.config.telegram.allowed_user_ids
    return event.from_user.id in allowed

# Keyboards
def get_main_keyboard() -> ReplyKeyboardMarkup:
    btn_start = KeyboardButton(text="🚀 СТАРТ")
    btn_stop = KeyboardButton(text="🛑 СТОП")
    btn_delistings = KeyboardButton(text="⚠️ Делистинги")
    btn_active_positions = KeyboardButton(text="📊 Активные Позиции")
    btn_notifications = KeyboardButton(text="🔔 Настройки оповещений")
    
    keyboard = [
        [btn_start, btn_stop],
        [btn_delistings, btn_active_positions],
        [btn_notifications]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_notifications_keyboard(cfg: Any) -> InlineKeyboardMarkup:
    tg_status = "✅" if cfg.telegram_alerts.enabled else "❌"
    po_status = "✅" if cfg.notifier_android.enabled else "❌"
    al_status = "✅" if cfg.notifier_apple2.enabled else "❌"
    te_status = "✅" if cfg.notifier_apple.enabled else "❌"
    
    keyboard = [
        [InlineKeyboardButton(text=f"Telegram-алерты: {tg_status}", callback_data="toggle_alerts:telegram_alerts")],
        [InlineKeyboardButton(text=f"Pushover (Android): {po_status}", callback_data="toggle_alerts:pushover_android")],
        [InlineKeyboardButton(text=f"Alertzy (iOS): {al_status}", callback_data="toggle_alerts:alertzy_ios")],
        [InlineKeyboardButton(text=f"Techulus (iOS): {te_status}", callback_data="toggle_alerts:techulus_ios")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_settings")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# Command Handlers
@idempotent_handler
async def cmd_start_status(message: types.Message):
    if not is_allowed(message):
        return
    
    from CORE.bot import ctx
    is_running = ctx.is_monitoring
    
    status_text = "<b>АКТИВЕН</b> 🟢" if is_running else "<b>ОСТАНОВЛЕН</b> 🛑"
    
    msg = (
        f"🤖 <b>Delisting Detector Bot Status</b>\n\n"
        f"Мониторинг: {status_text}\n"
        f"База делистингов биржи: {len(ctx.delisted_symbols)} монет(ы)\n"
        f"Активные монеты на мониторинге: {len(ctx.detector.active_symbols) if ctx.detector else 0} шт.\n"
        f"Совпадений/Алертов: {len(ctx.active_alerts)} шт."
    )
    await message.answer(msg, reply_markup=get_main_keyboard())

@idempotent_handler
async def handle_start_click(message: types.Message):
    if not is_allowed(message):
        return
    
    from CORE.bot import ctx, start_monitoring_services
    if ctx.is_monitoring:
        await message.answer("⚠️ Мониторинг делистингов уже запущен и работает в фоновом режиме.")
        return
        
    await start_monitoring_services()
    await message.answer("🚀 Мониторинг делистингов успешно запущен!", reply_markup=get_main_keyboard())

@idempotent_handler
async def handle_stop_click(message: types.Message):
    if not is_allowed(message):
        return
    
    from CORE.bot import ctx, stop_monitoring_services
    if not ctx.is_monitoring:
        await message.answer("⚠️ Мониторинг делистингов уже остановлен.")
        return
        
    await stop_monitoring_services()
    await message.answer("🛑 Мониторинг делистингов остановлен.", reply_markup=get_main_keyboard())

@idempotent_handler
async def handle_delistings_click(message: types.Message):
    if not is_allowed(message):
        return
    
    from CORE.bot import ctx
    await message.answer("⏳ Запрос актуального списка делистингов Phemex...")
    
    # Одноразовый REST-запрос на публичные продукты
    try:
        symbols = await ctx.symbols_api.get_all(only_active=False)
        delistings = sorted([
            sym.symbol.removesuffix("USDT")
            for sym in symbols if str(sym.status or "").strip() == "Delisted"
        ])
    except Exception as e:
        await message.answer(f"❌ Ошибка запроса: {e}")
        return
    
    if not delistings:
        await message.answer("✅ На бирже Phemex сейчас нет делистинговых монет (все активные).")
        return
        
    chunks = [delistings[i:i + 5] for i in range(0, len(delistings), 5)]
    formatted_chunks = [", ".join(chunk) for chunk in chunks]
    msg = f"⚠️ <b>Монеты в делистинге на Phemex ({len(delistings)} шт):</b>\n\n"
    msg += "\n".join(f"• {chunk}" for chunk in formatted_chunks)
    await message.answer(msg)

@idempotent_handler
async def handle_active_positions_click(message: types.Message):
    import asyncio
    if not is_allowed(message):
        return
    
    from CORE.bot import ctx
    if not ctx.private_client:
        await message.answer("❌ REST-клиент не инициализирован.")
        return
        
    await message.answer("⏳ Запрос активных позиций и ордеров через REST fallback API...")
    
    try:
        # 1. Получаем активные позиции (USDT)
        pos_res = await ctx.private_client.get_active_positions()
        positions_list = []
        if isinstance(pos_res, dict):
            p_data = pos_res.get("data")
            if isinstance(p_data, dict):
                positions_list = p_data.get("positions") or []
            elif isinstance(p_data, list):
                positions_list = p_data
        
        active_positions = []
        position_symbols = set()
        for pos in positions_list:
            symbol = pos.get("symbol")
            if not symbol:
                continue
            side = pos.get("side", "None")
            size = float(pos.get("size", 0) or pos.get("sizeRv", 0) or 0)
            if side != "None" and size > 0:
                active_positions.append({
                    "symbol": symbol,
                    "side": side,
                    "size": size
                })
                position_symbols.add(symbol)
                
        # 2. Определяем список символов для проверки ордеров (позиции + кэш детектора)
        # ВАЖНО: Нельзя добавлять delisted_symbols, иначе полетят 278 REST запросов и 429 Too Many Requests
        symbols_to_check = position_symbols.copy()
        if ctx.detector:
            symbols_to_check = symbols_to_check.union(ctx.detector.active_symbols)
            
        active_orders = []
        conditional_orders = []
        
        # 3. Выполняем запросы для каждого символа с ограничением конкурентности
        semaphore = asyncio.Semaphore(10)
        
        async def fetch_orders_for_symbol(symbol: str):
            async with semaphore:
                try:
                    ord_res = await ctx.private_client.get_active_orders(symbol)
                    ord_data = ord_res.get("data")
                    if isinstance(ord_data, dict):
                        ord_list = ord_data.get("rows", [])
                    elif isinstance(ord_data, list):
                        ord_list = ord_data
                    else:
                        ord_list = []
                        
                    for o in ord_list:
                        active_orders.append({
                            "symbol": symbol,
                            "orderID": o.get("orderID"),
                            "side": o.get("side"),
                            "price": o.get("priceRp") or o.get("price"),
                            "qty": o.get("orderQtyRq") or o.get("orderQty")
                        })
                except Exception as e:
                    logger.warning("Failed to fetch active orders for %s: %s", symbol, e)
                    
                try:
                    cond_res = await ctx.private_client.get_conditional_orders(symbol)
                    cond_data = cond_res.get("data")
                    if isinstance(cond_data, dict):
                        cond_list = cond_data.get("rows", [])
                    elif isinstance(cond_data, list):
                        cond_list = cond_data
                    else:
                        cond_list = []
                        
                    for o in cond_list:
                        conditional_orders.append({
                            "symbol": symbol,
                            "orderID": o.get("orderID"),
                            "side": o.get("side"),
                            "price": o.get("priceRp") or o.get("price"),
                            "qty": o.get("orderQtyRq") or o.get("orderQty"),
                            "trigger": o.get("triggerPriceRp") or o.get("triggerPrice")
                        })
                except Exception as e:
                    logger.warning("Failed to fetch conditional orders for %s: %s", symbol, e)

        # Собираем данные по ордерам
        if symbols_to_check:
            await asyncio.gather(*(fetch_orders_for_symbol(sym) for sym in symbols_to_check))
            
        # 4. Форматируем отчет по трем категориям
        msg = "📊 <b>Активные позиции и ордера (REST fallback):</b>\n\n"
        
        # Категория 1: Позиции
        msg += f"1️⃣ <b>Активные позиции ({len(active_positions)} шт):</b>\n"
        if active_positions:
            for p in sorted(active_positions, key=lambda x: x["symbol"]):
                sym = p["symbol"]
                warn = "⚠️ " if sym in delisted_symbols else "• "
                suffix = " <b>(ДЕЛИСТИНГ!)</b>" if sym in delisted_symbols else ""
                msg += f"{warn}{sym}: {p['side']}, {p['size']}{suffix}\n"
        else:
            msg += "• <i>нет активных позиций</i>\n"
            
        msg += "\n"
        
        # Категория 2: Лимитные ордера
        msg += f"2️⃣ <b>Активные лимитные ордера ({len(active_orders)} шт):</b>\n"
        if active_orders:
            for o in sorted(active_orders, key=lambda x: x["symbol"]):
                sym = o["symbol"]
                warn = "⚠️ " if sym in delisted_symbols else "• "
                suffix = " <b>(ДЕЛИСТИНГ!)</b>" if sym in delisted_symbols else ""
                msg += f"{warn}{sym}: {o['side']} Limit, Price: {o['price']}, Qty: {o['qty']}{suffix}\n"
        else:
            msg += "• <i>нет лимитных ордеров</i>\n"
            
        msg += "\n"
        
        # Категория 3: Условные ордера
        msg += f"3️⃣ <b>Активные условные ордера ({len(conditional_orders)} шт):</b>\n"
        if conditional_orders:
            for o in sorted(conditional_orders, key=lambda x: x["symbol"]):
                sym = o["symbol"]
                warn = "⚠️ " if sym in delisted_symbols else "• "
                suffix = " <b>(ДЕЛИСТИНГ!)</b>" if sym in delisted_symbols else ""
                msg += f"{warn}{sym}: {o['side']}, Trigger: {o['trigger']}, Qty: {o['qty']}{suffix}\n"
        else:
            msg += "• <i>нет условных ордеров</i>\n"
            
        await message.answer(msg)
        
    except Exception as e:
        logger.error("Error fetching active positions: %s", e)
        await message.answer(f"❌ Ошибка получения данных с биржи: {e}")

async def handle_notifications_click(message: types.Message):
    if not is_allowed(message):
        return
    
    from CORE.bot import ctx
    cfg = ctx.config_store.config
    await message.answer(
        "🔔 <b>Настройки каналов оповещений:</b>",
        reply_markup=get_notifications_keyboard(cfg)
    )

# Inline Callbacks
async def toggle_alerts_callback(call: types.CallbackQuery):
    if not is_allowed(call):
        await call.answer("Доступ запрещен", show_alert=True)
        return
        
    from CORE.bot import ctx
    channel = call.data.split(":")[1]
    
    cfg = ctx.config_store.config
    # Toggle current value
    current_val = False
    if channel == "telegram_alerts":
        current_val = cfg.telegram_alerts.enabled
    elif channel == "pushover_android":
        current_val = cfg.notifier_android.enabled
    elif channel == "alertzy_ios":
        current_val = cfg.notifier_apple2.enabled
    elif channel == "techulus_ios":
        current_val = cfg.notifier_apple.enabled
        
    new_val = not current_val
    ctx.config_store.update_alert_channel_enabled(channel, new_val)
    
    # Reload bot notifiers configuration to apply dynamically
    tg_enabled = ctx.config_store.config.telegram_alerts.enabled
    pushover_enabled = ctx.config_store.config.notifier_android.enabled
    techulus_enabled = ctx.config_store.config.notifier_apple.enabled
    alertzy_enabled = ctx.config_store.config.notifier_apple2.enabled
    
    ctx.notifier_manager.notifiers[0]._enabled = tg_enabled and bool(ctx.bot) and bool(ctx.config_store.config.telegram.allowed_user_ids)
    ctx.notifier_manager.notifiers[1]._enabled = pushover_enabled and bool(ctx.notifier_manager.notifiers[1].token)
    ctx.notifier_manager.notifiers[2]._enabled = techulus_enabled and bool(ctx.notifier_manager.notifiers[2].api_key)
    ctx.notifier_manager.notifiers[3]._enabled = alertzy_enabled and bool(ctx.notifier_manager.notifiers[3].account_key)
    
    await call.message.edit_reply_markup(
        reply_markup=get_notifications_keyboard(ctx.config_store.config)
    )
    await call.answer("Настройки обновлены!")

async def refresh_settings_callback(call: types.CallbackQuery):
    if not is_allowed(call):
        await call.answer("Доступ запрещен", show_alert=True)
        return
        
    from CORE.bot import ctx
    ctx.config_store.config = ctx.config_store.load()
    await call.message.edit_reply_markup(
        reply_markup=get_notifications_keyboard(ctx.config_store.config)
    )
    await call.answer("Конфигурация перезагружена с диска!")

# Registration
def register_handlers(dp: Dispatcher):
    # Commands
    dp.message.register(cmd_start_status, Command("start"))
    dp.message.register(cmd_start_status, Command("status"))
    dp.message.register(handle_start_click, Command("stop_monitoring")) # legacy name safety
    dp.message.register(handle_start_click, Command("start_monitoring"))
    
    # Text buttons
    dp.message.register(handle_start_click, F.text == "🚀 СТАРТ")
    dp.message.register(handle_stop_click, F.text == "🛑 СТОП")
    dp.message.register(handle_delistings_click, F.text == "⚠️ Делистинги")
    dp.message.register(handle_active_positions_click, F.text == "📊 Активные Позиции")
    dp.message.register(handle_notifications_click, F.text == "🔔 Настройки оповещений")
    
    # Callbacks
    dp.callback_query.register(toggle_alerts_callback, F.data.startswith("toggle_alerts:"))
    dp.callback_query.register(refresh_settings_callback, F.data == "refresh_settings")