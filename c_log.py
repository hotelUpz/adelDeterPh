# File: c_log.py
# Role: Unified logger with timezone support and file rotation.
from __future__ import annotations

import inspect
import logging
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
# import os
import time
from datetime import datetime
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional, Dict

import pytz

import consts

class _TzFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, consts.TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()

    def format(self, record):
        try:
            from consts import _store
            is_test = _store.config.app.test_mode
        except Exception:
            is_test = False

        if is_test:
            if isinstance(record.msg, str) and not record.msg.startswith("[SANDBOX_TEST]"):
                record.msg = f"[SANDBOX_TEST] {record.msg}"
        return super().format(record)



_all_logs_handler: Optional[RotatingFileHandler] = None

def get_all_logs_handler(log_dir: str | Path, approx_line_len: int = 350) -> RotatingFileHandler:
    global _all_logs_handler
    if _all_logs_handler is None:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        max_all_lines = getattr(consts, "MAX_ALL_LOGS_LINES", 100000)
        max_all_bytes = approx_line_len * max_all_lines
        all_log_path = Path(log_dir) / "all_logs.log"
        formatter = _TzFormatter("%(asctime)s | %(levelname)s | %(context)s | %(message)s", "%Y-%m-%d %H:%M:%S")
        _all_logs_handler = RotatingFileHandler(all_log_path, maxBytes=max_all_bytes, backupCount=2, encoding="utf-8")
        _all_logs_handler.setFormatter(formatter)
    return _all_logs_handler


class UnifiedLogger:
    def __init__(self, name: str, log_dir: str | Path = None, max_lines: int | None = None, context: Optional[str] = None, spam_throttle: float | None = None):
        if log_dir is None:
            try:
                from consts import _store
                if _store.config.app.test_mode:
                    log_dir = consts.BASE_DIR / "logs" / "test"
                else:
                    log_dir = consts.BASE_DIR / "logs"
            except Exception:
                log_dir = consts.BASE_DIR / "logs"
        if max_lines is None:
            max_lines = consts.MAX_LOG_LINES
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_path = Path(log_dir) / f"{name}.log"
        approx_line_len = 350
        max_bytes = max(100_000, approx_line_len * max_lines)

        base_logger = logging.getLogger(name)
        base_logger.setLevel(logging.DEBUG)
        base_logger.propagate = False

        if not base_logger.handlers:
            # Создаем единый форматер для всех выводов
            formatter = _TzFormatter("%(asctime)s | %(levelname)s | %(context)s | %(message)s", "%Y-%m-%d %H:%M:%S")

            # 1. Обработчик для записи в файл конкретного логгера
            file_handler = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=2, encoding="utf-8")
            file_handler.setFormatter(formatter)
            base_logger.addHandler(file_handler)

            # 2. Обработчик для записи всех логов в all_logs.log
            all_handler = get_all_logs_handler(log_dir, approx_line_len)
            max_all_lines = getattr(consts, "MAX_ALL_LOGS_LINES", 100000)
            all_handler.maxBytes = approx_line_len * max_all_lines
            base_logger.addHandler(all_handler)

            # 3. Обработчик для вывода в консоль (заменяет print)
            if consts.LOG_DEBUG: 
                console_handler = logging.StreamHandler(sys.stdout)
                console_handler.setFormatter(formatter)
                base_logger.addHandler(console_handler)

        self._logger = logging.LoggerAdapter(base_logger, extra={"context": context or name})
        self._last_logs: Dict[str, float] = {}
        self._spam_throttle = spam_throttle if spam_throttle is not None else getattr(consts, "SPAM_THROTTLE_SEC", 60.0)

    def _check_spam(self, msg: str) -> bool:
        now = time.time()
        if msg in self._last_logs:
            if now - self._last_logs[msg] < self._spam_throttle:
                return True
        self._last_logs[msg] = now
        return False

    def debug(self, msg: str, *args, **kwargs) -> None:
        if consts.LOG_DEBUG:
            formatted_msg = msg % args if args else msg
            if self._check_spam(formatted_msg): return
            self._logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs) -> None:
        if consts.LOG_INFO:
            formatted_msg = msg % args if args else msg
            if self._check_spam(formatted_msg): return
            self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs) -> None:
        if consts.LOG_WARNING:
            formatted_msg = msg % args if args else msg
            if self._check_spam(formatted_msg): return
            self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs) -> None:
        if consts.LOG_ERROR:
            formatted_msg = msg % args if args else msg
            if self._check_spam(formatted_msg): return
            self._logger.error(msg, *args, **kwargs)

    def exception(self, msg: str, *args, **kwargs) -> None:
        if consts.LOG_ERROR:
            # Для exception msg обычно фиксированный, а детали в стектрейсе.
            # Но мы проверим сам msg.
            if self._check_spam(msg): return
            self._logger.exception(msg, *args, **kwargs)

    def total_exception_decor(self, func, context: Optional[Any] = None):
        if getattr(func, "_is_wrapped", False):
            return func

        if context is not None:
            target_logger = logging.LoggerAdapter(self._logger.logger, extra={"context": context})
        else:
            target_logger = self._logger

        if hasattr(func, "__call__"):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception:
                    target_logger.exception("Unhandled async exception in %s", getattr(func, "__qualname__", repr(func)))
                    return None

            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except Exception:
                    target_logger.exception("Unhandled sync exception in %s", getattr(func, "__qualname__", repr(func)))
                    return None

            wrapper = async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper
            wrapper._is_wrapped = True
            return wrapper
        return func