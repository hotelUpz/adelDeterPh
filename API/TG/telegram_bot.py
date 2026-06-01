# ============================================================
# FILE: API/TG/telegram_bot.py
# ROLE: Telegram Bot UI Handlers — обработчики команд пользователя.
# ============================================================

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

def is_allowed(event: types.Message | types.CallbackQuery, ctx: Any) -> bool:
    if not ctx.config_store:
        return False
    allowed = ctx.config_store.config.telegram.allowed_user_ids
    return event.from_user.id in allowed

def get_main_keyboard() -> ReplyKeyboardMarkup:
    btn_start = KeyboardButton(text="🚀 СТАРТ")
    btn_stop = KeyboardButton(text="🛑 СТОП")
    btn_status = KeyboardButton(text="📊 Статус")
    btn_notifications = KeyboardButton(text="🔔 Уведомления")

    keyboard = [
        [btn_start, btn_stop],
        [btn_status, btn_notifications],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_notifications_keyboard(cfg: Any) -> InlineKeyboardMarkup:
    tg_status = "🟢 Вкл" if cfg.telegram_alerts.enabled else "🔴 Выкл"
    po_status = "🟢 Вкл" if cfg.notifier_android.enabled else "🔴 Выкл"
    al_status = "🟢 Вкл" if cfg.notifier_apple2.enabled else "🔴 Выкл"
    te_status = "🟢 Вкл" if cfg.notifier_apple.enabled else "🔴 Выкл"
    
    keyboard = [
        [
            InlineKeyboardButton(text=f"Telegram: {tg_status}", callback_data="toggle_alerts:telegram_alerts"),
            InlineKeyboardButton(text=f"Pushover: {po_status}", callback_data="toggle_alerts:pushover_android")
        ],
        [
            InlineKeyboardButton(text=f"Alertzy: {al_status}", callback_data="toggle_alerts:alertzy_ios"),
            InlineKeyboardButton(text=f"Techulus: {te_status}", callback_data="toggle_alerts:techulus_ios")
        ],
        [InlineKeyboardButton(text="🔙 Закрыть", callback_data="close_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_notifications_text(cfg: Any) -> str:
    tg_status = "🟢 Вкл" if cfg.telegram_alerts.enabled else "🔴 Выкл"
    po_status = "🟢 Вкл" if cfg.notifier_android.enabled else "🔴 Выкл"
    al_status = "🟢 Вкл" if cfg.notifier_apple2.enabled else "🔴 Выкл"
    te_status = "🟢 Вкл" if cfg.notifier_apple.enabled else "🔴 Выкл"
    
    return (
        "🔔 <b>Управление уведомлениями и пушами</b>\n\n"
        f"• Telegram-алерты: {tg_status}\n"
        f"• Pushover (Android): {po_status}\n"
        f"• Alertzy (iOS): {al_status}\n"
        f"• Techulus (iOS): {te_status}\n\n"
        "Нажмите на соответствующую кнопку ниже для переключения статуса канала:"
    )

@idempotent_handler
async def cmd_start_status(message: types.Message, ctx: Any):
    if not is_allowed(message, ctx):
        return
    
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
async def handle_start_click(message: types.Message, ctx: Any):
    if not is_allowed(message, ctx):
        return
    
    if ctx.is_monitoring:
        await message.answer("⚠️ Мониторинг делистингов уже запущен и работает в фоновом режиме.")
        return
        
    await ctx.start_monitoring()
    await message.answer("🚀 Мониторинг делистингов успешно запущен!", reply_markup=get_main_keyboard())

@idempotent_handler
async def handle_stop_click(message: types.Message, ctx: Any):
    if not is_allowed(message, ctx):
        return
    
    if not ctx.is_monitoring:
        await message.answer("⚠️ Мониторинг делистингов уже остановлен.")
        return
        
    await ctx.stop_monitoring()
    await message.answer("🛑 Мониторинг делистингов остановлен.", reply_markup=get_main_keyboard())

async def handle_notifications_click(message: types.Message, ctx: Any):
    if not is_allowed(message, ctx):
        return
    
    cfg = ctx.config_store.config
    text = get_notifications_text(cfg)
    await message.answer(
        text,
        reply_markup=get_notifications_keyboard(cfg),
        parse_mode="HTML"
    )

async def toggle_alerts_callback(call: types.CallbackQuery, ctx: Any):
    if not is_allowed(call, ctx):
        await call.answer("Доступ запрещен", show_alert=True)
        return
        
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
    
    cfg = ctx.config_store.config
    text = get_notifications_text(cfg)
    await call.message.edit_text(
        text=text,
        reply_markup=get_notifications_keyboard(cfg),
        parse_mode="HTML"
    )
    await call.answer("Настройки обновлены!")

async def close_menu_callback(call: types.CallbackQuery, ctx: Any):
    if not is_allowed(call, ctx):
        return
    await call.message.delete()

async def refresh_settings_callback(call: types.CallbackQuery, ctx: Any):
    if not is_allowed(call, ctx):
        await call.answer("Доступ запрещен", show_alert=True)
        return
        
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
    dp.message.register(handle_notifications_click, F.text == "🔔 Уведомления")
    dp.message.register(cmd_start_status, F.text == "📊 Статус")

    dp.callback_query.register(toggle_alerts_callback, F.data.startswith("toggle_alerts:"))
    dp.callback_query.register(close_menu_callback, F.data == "close_menu")