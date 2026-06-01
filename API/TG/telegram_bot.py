# File: API/TG/telegram_bot.py
# Role: Telegram Bot UI Handlers — только боевые кнопки Старт/Стоп/Оповещения.
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

def is_allowed(event: types.Message | types.CallbackQuery) -> bool:
    from CORE.bot import ctx
    if not ctx.config_store:
        return False
    allowed = ctx.config_store.config.telegram.allowed_user_ids
    return event.from_user.id in allowed

# Главная клавиатура — строго 3 кнопки по ТЗ
def get_main_keyboard() -> ReplyKeyboardMarkup:
    btn_start         = KeyboardButton(text="🚀 СТАРТ")
    btn_stop          = KeyboardButton(text="🛑 СТОП")
    btn_notifications = KeyboardButton(text="🔔 Настройки оповещений")

    keyboard = [
        [btn_start, btn_stop],
        [btn_notifications],
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

async def handle_notifications_click(message: types.Message):
    if not is_allowed(message):
        return
    
    from CORE.bot import ctx
    cfg = ctx.config_store.config
    await message.answer(
        "🔔 <b>Настройки каналов оповещений:</b>",
        reply_markup=get_notifications_keyboard(cfg)
    )

async def toggle_alerts_callback(call: types.CallbackQuery):
    if not is_allowed(call):
        await call.answer("Доступ запрещен", show_alert=True)
        return
        
    from CORE.bot import ctx
    channel = call.data.split(":")[1]
    
    cfg = ctx.config_store.config
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
    
    # Синхронизируем состояние включения в менеджере нотификаций
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

def register_handlers(dp: Dispatcher):
    dp.message.register(cmd_start_status, Command("start"))
    dp.message.register(cmd_start_status, Command("status"))
    dp.message.register(handle_start_click, Command("start_monitoring"))
    dp.message.register(handle_stop_click, Command("stop_monitoring"))

    dp.message.register(handle_start_click, F.text == "🚀 СТАРТ")
    dp.message.register(handle_stop_click, F.text == "🛑 СТОП")
    dp.message.register(handle_notifications_click, F.text == "🔔 Настройки оповещений")

    dp.callback_query.register(toggle_alerts_callback, F.data.startswith("toggle_alerts:"))
    dp.callback_query.register(refresh_settings_callback, F.data == "refresh_settings")