# ============================================================
# FILE: consts.py
# ROLE: Строгий загрузчик конфигураций, констант и стейта.
# ============================================================

import json
from pathlib import Path
import pytz

BASE_DIR = Path(__file__).resolve().parent

# --- Классы валидации конфигурационных данных (явное чтение без неявных get) ---

class AppConfig:
    def __init__(self, data: dict):
        self.game_loop_interval_sec = int(data["game_loop_interval_sec"])
        self.telegram_burst_delay_sec = float(data["telegram_burst_delay_sec"])
        self.push_burst_delay_sec = float(data["push_burst_delay_sec"])

class EmailMonitorConfig:
    def __init__(self, data: dict):
        self.enabled = bool(data["enabled"])
        self.poll_interval_sec = int(data["poll_interval_sec"])
        self.max_messages = int(data["max_messages"])
        self.imap_server = str(data["imap_server"])
        self.sender_filter = str(data["sender_filter"])

class LoggerConfig:
    def __init__(self, data: dict):
        self.log_debug_debug = bool(data.get("log_debug_debug", False))
        self.log_debug = bool(data["log_debug"])
        self.log_error = bool(data["log_error"])
        self.log_info = bool(data["log_info"])
        self.log_warning = bool(data["log_warning"])
        self.max_log_lines = int(data["max_log_lines"])
        self.time_zone = str(data["time_zone"])
        self.spam_throttle_sec = float(data["spam_throttle_sec"])

class AlertChannelConfig:
    def __init__(self, data: dict):
        self.enabled = bool(data["enabled"])

class FullConfig:
    def __init__(self, app_data: dict, alerts_data: dict, tg_data: dict):
        # Храним сырой словарь для совместимости с notifier.py / telegram_bot.py
        self.raw = {**app_data, **alerts_data, **tg_data}
        
        # Объекты строгого маппинга параметров
        self.app = AppConfig(app_data["app"])
        self.email_monitor = EmailMonitorConfig(app_data["email_monitor"])
        self.logger = LoggerConfig(app_data["logger"])
        self.telegram_alerts = AlertChannelConfig(alerts_data["telegram_alerts"])
        self.notifier_android = AlertChannelConfig(alerts_data["pushover_android"])
        self.notifier_apple = AlertChannelConfig(alerts_data["techulus_ios"])
        self.notifier_apple2 = AlertChannelConfig(alerts_data["alertzy_ios"])
        self.notifier_join = AlertChannelConfig(alerts_data.get("join_android", {"enabled": False}))
        
        # Секция Telegram бота
        from dataclasses import dataclass
        @dataclass
        class TGSection:
            enabled: bool
            allowed_user_ids: list[int]
            
        self.telegram = TGSection(
            enabled=bool(tg_data["telegram"]["enabled"]),
            allowed_user_ids=[int(uid) for uid in tg_data["telegram"]["allowed_user_ids"]]
        )
        
        # Настройки логики повторов алертов делистинга
        self.delisting_repeats = int(alerts_data["delisting_repeats"])
        self.delisting_interval_sec = float(alerts_data["delisting_interval_sec"])
        self.alert_title_template = str(alerts_data["alert_title_template"])
        self.alert_body_template = str(alerts_data["alert_body_template"])

class ConfigStore:
    def __init__(self, config_dir_path: str = "CONFIG/prod"):
        p = Path(config_dir_path)
        self.config_dir = p if p.is_absolute() else BASE_DIR / config_dir_path
        self.config = self.load()

    def load(self) -> FullConfig:
        with open(self.config_dir / "app.json", "r", encoding="utf-8") as f:
            app_data = json.load(f)
        with open(self.config_dir / "alerts.json", "r", encoding="utf-8") as f:
            alerts_data = json.load(f)
        with open(self.config_dir / "telegram.json", "r", encoding="utf-8") as f:
            tg_data = json.load(f)
        return FullConfig(app_data, alerts_data, tg_data)

    def update_alert_channel_enabled(self, channel: str, enabled: bool):
        """Прямая перезапись флага канала в alerts.json и обновление кэша в памяти."""
        alerts_file = self.config_dir / "alerts.json"
        with open(alerts_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        if channel in data:
            data[channel]["enabled"] = enabled
            
        with open(alerts_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            
        self.config = self.load()

# --- Первичный статический импорт для глобальных переменных логгера (c_log.py) ---
try:
    with open(BASE_DIR / "CONFIG/prod/app.json", "r", encoding="utf-8") as _f:
        _app_data = json.load(_f)
    LOG_DEBUG_DEBUG = bool(_app_data["logger"].get("log_debug_debug", False))
    LOG_DEBUG = bool(_app_data["logger"]["log_debug"])
    LOG_INFO = bool(_app_data["logger"]["log_info"])
    LOG_WARNING = bool(_app_data["logger"]["log_warning"])
    LOG_ERROR = bool(_app_data["logger"]["log_error"])
    MAX_LOG_LINES = int(_app_data["logger"]["max_log_lines"])
    SPAM_THROTTLE_SEC = float(_app_data["logger"]["spam_throttle_sec"])
    TZ = pytz.timezone(_app_data["logger"]["time_zone"])
except Exception:
    LOG_DEBUG_DEBUG = False
    LOG_DEBUG, LOG_INFO, LOG_WARNING, LOG_ERROR = True, True, True, True
    MAX_LOG_LINES = 10000
    SPAM_THROTTLE_SEC = 60.0
    TZ = pytz.utc

MAX_ALL_LOGS_LINES = 100000

# Глобальный синглтон хранилища для модулей уведомлений
_store = ConfigStore("CONFIG/prod")