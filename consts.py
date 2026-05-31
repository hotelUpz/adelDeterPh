# File: consts.py
# Role: Configuration management, global constants, and legacy bridge.
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytz

BASE_DIR = Path(__file__).resolve().parent

# Default global variables (fallbacks)
CURRENCY = "USDT"
LOG_DEBUG = True
LOG_ERROR = True
LOG_INFO = True
LOG_WARNING = True
MAX_LOG_LINES = 100
MAX_ALL_LOGS_LINES = 100000
TIME_ZONE = "UTC"
TZ = pytz.timezone(TIME_ZONE)
SPAM_THROTTLE_SEC = 60.0

class AppConfig:
    def __init__(self, raw: dict):
        self.delisting_poll_interval_sec = float(raw.get("delisting_poll_interval_sec", 5.0))
        self.game_loop_interval_sec = float(raw.get("game_loop_interval_sec", 20.0))
        self.telegram_burst_delay_sec = float(raw.get("telegram_burst_delay_sec", 1.0))
        self.push_burst_delay_sec = float(raw.get("push_burst_delay_sec", 30.0))

class AlertChannelConfig:
    def __init__(self, raw: dict):
        self.enabled = bool(raw.get("enabled", False))

class TelegramConfig:
    def __init__(self, raw: dict):
        self.enabled = bool(raw.get("enabled", False))
        self.allowed_user_ids = list(raw.get("allowed_user_ids", []))

class Config:
    def __init__(self, raw_app: dict, raw_alerts: dict, raw_tg: dict):
        self.raw = {
            "app": raw_app,
            "alerts": raw_alerts,
            "telegram": raw_tg
        }
        self.app = AppConfig(raw_app)
        self.telegram_alerts = AlertChannelConfig(raw_alerts.get("telegram_alerts", {}))
        self.notifier_android = AlertChannelConfig(raw_alerts.get("pushover_android", {}))
        self.notifier_apple = AlertChannelConfig(raw_alerts.get("techulus_ios", {}))
        self.notifier_apple2 = AlertChannelConfig(raw_alerts.get("alertzy_ios", {}))
        self.telegram = TelegramConfig(raw_tg)
        
        self.delisting_repeats = int(raw_alerts.get("delisting_repeats", 3))
        self.delisting_interval_sec = float(raw_alerts.get("delisting_interval_sec", 60.0))
        self.alert_title_template = str(raw_alerts.get("alert_title_template", "⚠️ ОБНАРУЖЕН ДЕЛИСТИНГ: {symbol}"))
        self.alert_body_template = str(raw_alerts.get("alert_body_template", "Внимание! Актив <b>{symbol}</b> находится в процессе делистинга на Phemex, но по нему обнаружена открытая позиция или активный ордер!\n\nПовтор {repeat_num} из {total_repeats}."))

class ConfigStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.config = self.load()

    def load(self) -> Config:
        app_file = self.path / "app.json"
        alerts_file = self.path / "alerts.json"
        tg_file = self.path / "telegram.json"
        
        raw_app = {}
        if app_file.exists():
            try:
                with open(app_file, "r", encoding="utf-8") as f:
                    raw_app = json.load(f).get("app", {})
            except Exception:
                pass
        
        raw_alerts = {}
        if alerts_file.exists():
            try:
                with open(alerts_file, "r", encoding="utf-8") as f:
                    raw_alerts = json.load(f)
            except Exception:
                pass
                
        raw_tg = {}
        if tg_file.exists():
            try:
                with open(tg_file, "r", encoding="utf-8") as f:
                    raw_tg = json.load(f).get("telegram", {})
            except Exception:
                pass
                
        return Config(raw_app, raw_alerts, raw_tg)

    def update_alert_channel_enabled(self, channel: str, value: bool):
        alerts_file = self.path / "alerts.json"
        raw_alerts = {}
        if alerts_file.exists():
            try:
                with open(alerts_file, "r", encoding="utf-8") as f:
                    raw_alerts = json.load(f)
            except Exception:
                pass
        
        key_map = {
            "telegram_alerts": "telegram_alerts",
            "pushover_android": "pushover_android",
            "techulus_ios": "techulus_ios",
            "alertzy_ios": "alertzy_ios"
        }
        json_key = key_map.get(channel, channel)
        if json_key not in raw_alerts:
            raw_alerts[json_key] = {}
        raw_alerts[json_key]["enabled"] = value
        
        try:
            with open(alerts_file, "w", encoding="utf-8") as f:
                json.dump(raw_alerts, f, indent=4)
        except Exception:
            pass
            
        self.config = self.load()

_store: ConfigStore | None = None
