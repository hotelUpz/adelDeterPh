# ============================================================
# FILE: CORE/bot.py
# ROLE: Оркестратор — RAM-кэш, батчинг сигналов и гейм-лупа.
# ============================================================

import asyncio
import time
from typing import Optional, Set, Dict, Any, List
from collections import defaultdict

from consts import ConfigStore, FullConfig
from c_log import UnifiedLogger

# Прямой импорт оригинальных модулей Phemex без оберток
from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.get_pos_symbols import PhemexPrivateRESTFallback

logger = UnifiedLogger("orchestrator")

# --- СОВМЕСТИМОСТЬ С ЛЕГАСИ-ВЫЗОВАМИ ИЗ ТЕЛЕГРАМ-БОТА ---
_orig_get_all = PhemexSymbols.get_all
PhemexSymbols.get_all = lambda self, only_active=False, **kw: _orig_get_all(self, quote="USDT")
PhemexPrivateRESTFallback.get_active_positions = lambda self, *a, **kw: {"code": 0, "data": {"positions": []}}
PhemexPrivateRESTFallback.get_active_orders = lambda self, *a, **kw: {"code": 0, "data": {"rows": []}}
PhemexPrivateRESTFallback.get_conditional_orders = lambda self, *a, **kw: {"code": 0, "data": {"rows": []}}


class DetectorState:
    """Для хранения задействованных монет в RAM (совместимость со статус-командой UI)."""
    def __init__(self) -> None:
        self.active_symbols: Set[str] = set()


class DelistingOrchestrator:
    """Архитектурное ядро — инкапсулирует состояние, RAM-кэш и фоновый цикл."""
    def __init__(self) -> None:
        import consts
        self.config_store: ConfigStore = consts._store
        self.is_monitoring: bool = False
        
        # Кэш сигналов и состояний исключительно в оперативной памяти инстанса (RAM)
        self.delisted_symbols: Set[str] = set()   
        self.detector: DetectorState = DetectorState() 
        self.active_alerts: Dict[str, Dict[str, Any]] = {}         
        self.tracked_matches: Set[str] = set()    
        
        # Зависимости верхнего уровня (инжектируются через main.py)
        self.bot = None
        self.symbols_api: Optional[PhemexSymbols] = None
        self.private_client: Optional[PhemexPrivateRESTFallback] = None
        self.notifier_manager = None
        
        self._loop_task: Optional[asyncio.Task[None]] = None

    def inject_dependencies(self, bot, symbols_api: PhemexSymbols, private_client: PhemexPrivateRESTFallback, notifier_manager) -> None:
        """Инъекция зависимостей после импорта для избежания циклических ссылок."""
        self.bot = bot
        self.symbols_api = symbols_api
        self.private_client = private_client
        self.notifier_manager = notifier_manager

    @staticmethod
    def normalize_symbol(sym: Any) -> str:
        """Универсальная нормализация тикера инструмента."""
        return str(sym).upper().removesuffix("USDT").strip()

    @staticmethod
    def get_intersection(set_a: Set[str], set_b: Set[str]) -> Set[str]:
        return set_a.intersection(set_b)

    async def start_monitoring(self) -> None:
        if self.is_monitoring: return
        self.is_monitoring = True
        self._loop_task = asyncio.create_task(self.run_game_loop())
        logger.info("Гейм-лупа фонового сканирования позиций успешно запущена.")

    async def stop_monitoring(self) -> None:
        if not self.is_monitoring: return
        self.is_monitoring = False
        if self._loop_task:
            self._loop_task.cancel()
            try: await self._loop_task
            except asyncio.CancelledError: pass
            self._loop_task = None
        logger.info("Гейм-лупа фонового сканирования позиций остановлена.")

    async def run_game_loop(self) -> None:
        while self.is_monitoring:
            try:
                # Динамически получаем легкую ссылку на актуальный конфиг (отражает изменения из UI/файла)
                cfg: FullConfig = self.config_store.config
                now = time.time()

                # ШАГ 1: Дергаем публичные делистинги
                delist_rows = await self.symbols_api.get_asoon_delisting_symbols()
                if not delist_rows:
                    await asyncio.sleep(cfg.app.game_loop_interval_sec)
                    continue

                # ШАГ 3: Нормализация делистинг-символов
                self.delisted_symbols = {self.normalize_symbol(r.symbol) for r in delist_rows}

                # ШАГ 1 (продолжение): Дергаем приватные позы
                active_raw = await self.private_client.get_active_symbols()
                if not active_raw:
                    self.detector.active_symbols = set()
                    await asyncio.sleep(cfg.app.game_loop_interval_sec)
                    continue

                # ШАГ 3: Нормализация активных символов
                self.detector.active_symbols = {self.normalize_symbol(s) for s in active_raw}

                # ШАГ 4 & 5: Изолированное сравнение множеств
                current_matches = self.get_intersection(self.detector.active_symbols, self.delisted_symbols)
                new_signals = current_matches - self.tracked_matches

                # БАТЧИНГ: Группировка символов по номеру повтора (repeat_num) для экономии API лимитов
                alerts_to_send: Dict[int, List[str]] = defaultdict(list)

                for sym in current_matches:
                    if sym in new_signals:
                        # Новый сигнал (repeat = 1)
                        self.active_alerts[sym] = {"repeat_num": 1, "last_sent": now}
                        alerts_to_send[1].append(sym)
                    else:
                        # Существующий сигнал: проверка пауз и лимитов повторов
                        alert = self.active_alerts.get(sym)
                        if alert and alert["repeat_num"] < cfg.delisting_repeats:
                            if now - alert["last_sent"] >= cfg.delisting_interval_sec:
                                alert["repeat_num"] += 1
                                alert["last_sent"] = now
                                alerts_to_send[alert["repeat_num"]].append(sym)

                # Отправка сгруппированных пушей (один запрос на каждую группу repeat_num)
                for repeat_num, syms_list in alerts_to_send.items():
                    joined_symbols = ", ".join(syms_list)
                    await self._dispatch_alert(joined_symbols, repeat_num, cfg)

                # Очистка RAM кэша от закрытых позиций
                closed_symbols = self.active_alerts.keys() - current_matches
                for sym in closed_symbols:
                    logger.info("Позиция по %s успешно закрыта на бирже. Удаляем из RAM кэша.", sym)
                    del self.active_alerts[sym]

                # ШАГ 8: Обновление стейта прошлой итерации
                self.tracked_matches = current_matches

            except Exception as e:
                logger.error("Ошибка итерации гейм-лупы: %s", e)

            # Пауза итерации согласно конфигу
            await asyncio.sleep(self.config_store.config.app.game_loop_interval_sec)

    async def _dispatch_alert(self, symbols_str: str, repeat_num: int, cfg: FullConfig) -> None:
        """Рендер шаблона и рассылка сгруппированного списка символов."""
        try:
            title = cfg.alert_title_template.format(symbols=symbols_str)
            body = cfg.alert_body_template.format(symbols=symbols_str, repeat_num=repeat_num, total_repeats=cfg.delisting_repeats)
            
            await self.notifier_manager.send(title, body, priority=1)
            logger.warning("🚨 СИГНАЛ ОТПРАВЛЕН: %s [Повтор %d/%d]", symbols_str, repeat_num, cfg.delisting_repeats)
        except Exception as e:
            logger.error("Не удалось отправить нотификацию для %s: %s", symbols_str, e)



# Глобальный синглтон-контекст. Зависимости вливаются снаружи из main.py
ctx: DelistingOrchestrator = DelistingOrchestrator()

# Псевдонимы-мосты для кнопок UI (telegram_bot.py)
start_monitoring_services = ctx.start_monitoring
stop_monitoring_services = ctx.stop_monitoring