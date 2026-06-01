# ============================================================
# FILE: CORE/bot.py
# ROLE: Чистый оркестратор — RAM-кэширование, типизация и гейм-лупа.
# ============================================================

import asyncio
import time
from typing import Optional, Set, Dict, Any

from aiogram import Bot

from consts import ConfigStore
from c_log import UnifiedLogger

# Прямой импорт оригинальных модулей Phemex без оберток
from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.get_pos_symbols import PhemexPrivateRESTFallback
from API.TG.notifier import NotificationManager

logger = UnifiedLogger("orchestrator")


class DetectorState:
    """Для хранения задействованных монет в RAM (совместимость со статус-командой UI)."""
    def __init__(self) -> None:
        self.active_symbols: Set[str] = set()


class DelistingOrchestrator:
    """Архитектурное ядро — инкапсулирует состояние, RAM-кэш и фоновый цикл."""
    def __init__(self, config_store: ConfigStore, bot: Bot, symbols_api: PhemexSymbols, private_client: PhemexPrivateRESTFallback, notifier_manager: NotificationManager) -> None:
        self.config_store: ConfigStore = config_store
        cfg = self.config_store.config
        
        # Читаем все настройки строго один раз при инициализации класса
        self.loop_interval_sec: int = int(cfg.app.game_loop_interval_sec)
        self.delisting_repeats: int = int(cfg.delisting_repeats)
        self.delisting_interval_sec: float = float(cfg.delisting_interval_sec)
        self.title_template: str = str(cfg.alert_title_template)
        self.body_template: str = str(cfg.alert_body_template)
        
        self.is_monitoring: bool = False
        
        # Кэш сигналов и состояний исключительно в оперативной памяти инстанса (RAM)
        self.delisted_symbols: Set[str] = set()   
        self.detector: DetectorState = DetectorState() 
        self.active_alerts: Dict[str, Dict[str, Any]] = {}         
        self.tracked_matches: Set[str] = set()    
        
        # Инжектированные зависимости верхнего уровня
        self.bot: Bot = bot
        self.symbols_api: PhemexSymbols = symbols_api
        self.private_client: PhemexPrivateRESTFallback = private_client
        self.notifier_manager: NotificationManager = notifier_manager
        
        self._loop_task: Optional[asyncio.Task[None]] = None

    @staticmethod
    def get_intersection(set_a: Set[str], set_b: Set[str]) -> Set[str]:
        """Утилита для нахождения пересечений между множествами инструментов."""
        return set_a.intersection(set_b)

    @staticmethod
    def get_new_signals(current_matches: Set[str], tracked_matches: Set[str]) -> Set[str]:
        """Утилита для вычисления разницы множеств и нахождения новых сигналов."""
        return current_matches - tracked_matches

    async def start_monitoring(self) -> None:
        """Метод запуска фонового сканирования."""
        if self.is_monitoring:
            return
        self.is_monitoring = True
        self._loop_task = asyncio.create_task(self.run_game_loop())
        logger.info("Гейм-лупа фонового сканирования позиций успешно запущена.")

    async def stop_monitoring(self) -> None:
        """Метод контролируемой остановки фонового сканирования."""
        if not self.is_monitoring:
            return
        self.is_monitoring = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        logger.info("Гейм-лупа фонового сканирования позиций остановлена.")

    async def run_game_loop(self) -> None:
        """Оптимизированный фоновый цикл мониторинга по шагам ТЗ."""
        while self.is_monitoring:
            try:
                # ШАГ 1: Сначала дергаем публичные делистинги (экономим приватные API лимиты)
                delist_rows = await self.symbols_api.get_asoon_delisting_symbols()
                if not delist_rows:
                    await asyncio.sleep(self.loop_interval_sec)
                    continue

                # ШАГ 3: Нормализация делистинг-символов к единому формату (без USDT, UPPERCASE)
                delist_set = {str(r.symbol).upper().removesuffix("USDT").strip() for r in delist_rows}
                self.delisted_symbols = delist_set

                # ШАГ 1 (продолжение): Только при наличии делистингов проверяем позы на аккаунте
                active_raw = await self.private_client.get_active_symbols()
                if not active_raw:
                    self.detector.active_symbols = set()
                    await asyncio.sleep(self.loop_interval_sec)
                    continue

                # ШАГ 3: Нормализация активных символов
                active_set = {str(s).upper().removesuffix("USDT").strip() for s in active_raw}
                self.detector.active_symbols = active_set

                # ШАГ 4: Поиск совпадений общих символов через утилиту множеств
                current_matches = self.get_intersection(active_set, delist_set)

                # ШАГ 5: Находим новые сигналы-символы через разницу множеств
                new_signals = self.get_new_signals(current_matches, self.tracked_matches)

                now = time.time()

                # Линейный проход обработки алертов без дублирующих циклов и лишнего овера
                for sym in current_matches:
                    if sym in new_signals:
                        # Новый сигнал: мгновенно инициализируем в памяти и отправляем первый пуш
                        self.active_alerts[sym] = {"repeat_num": 1, "last_sent": now}
                        await self._dispatch_alert(sym, 1)
                    else:
                        # Существующий сигнал: проверяем лимиты повторов и интервалы пауз
                        alert = self.active_alerts.get(sym)
                        if alert and alert["repeat_num"] < self.delisting_repeats:
                            if now - alert["last_sent"] >= self.delisting_interval_sec:
                                alert["repeat_num"] += 1
                                alert["last_sent"] = now
                                await self._dispatch_alert(sym, alert["repeat_num"])

                # Вычищаем из оперативной памяти конфиги повторов по закрытым позициям
                closed_symbols = self.active_alerts.keys() - current_matches
                for sym in closed_symbols:
                    logger.info("Позиция по %s успешно закрыта на бирже. Удаляем из RAM кэша.", sym)
                    del self.active_alerts[sym]

                # ШАГ 8: Обновляем кэш совпадений в самом конце текущей итерации
                self.tracked_matches = current_matches

            except Exception as e:
                logger.error("Ошибка итерации гейм-лупы: %s", e)

            await asyncio.sleep(self.loop_interval_sec)

    async def _dispatch_alert(self, symbol: str, repeat_num: int) -> None:
        """Сборка шаблона сообщения и прямая отправка во все включенные пуш-каналы."""
        try:
            title = self.title_template.format(symbols=symbol)
            body = self.body_template.format(symbols=symbol, repeat_num=repeat_num, total_repeats=self.delisting_repeats)
            
            await self.notifier_manager.send(title, body, priority=1)
            logger.warning("🚨 ОБНАРУЖЕН ДЕЛИСТИНГ: %s [Повтор %d/%d]", symbol, repeat_num, self.delisting_repeats)
        except Exception as e:
            logger.error("Не удалось отправить нотификацию по символу %s: %s", symbol, e)