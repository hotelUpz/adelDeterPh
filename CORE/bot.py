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

from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.get_pos_symbols import PhemexPrivateRESTFallback
from API.GMAIL.gmail_monitor import GmailMonitor

logger = UnifiedLogger("orchestrator")

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
        
        # Кэш сигналов и состояний
        self.delisted_symbols: Set[str] = set()   
        self.known_delisted_symbols: Set[str] = set()
        self.detector: DetectorState = DetectorState() 
        self.active_alerts: Dict[str, Dict[str, Any]] = {}         
        self.tracked_matches: Set[str] = set()    
        
        # Зависимости верхнего уровня
        self.bot = None
        self.symbols_api: Optional[PhemexSymbols] = None
        self.private_client: Optional[PhemexPrivateRESTFallback] = None
        self.notifier_manager = None
        self.gmail_monitor: Optional[GmailMonitor] = None
        
        self._loop_task: Optional[asyncio.Task[None]] = None

    def inject_dependencies(self, bot, symbols_api: PhemexSymbols, private_client: PhemexPrivateRESTFallback, notifier_manager, gmail_monitor: GmailMonitor) -> None:
        self.bot = bot
        self.symbols_api = symbols_api
        self.private_client = private_client
        self.notifier_manager = notifier_manager
        self.gmail_monitor = gmail_monitor

    @staticmethod
    def _debug_debug(msg: str, *args):
        import consts
        if getattr(consts, "LOG_DEBUG_DEBUG", False):
            logger.debug(f"[DEBUG_DEBUG] {msg}", *args)

    @staticmethod
    def normalize_symbol(sym: Any) -> str:
        return str(sym).upper().removesuffix("USDT").strip()

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
                self._debug_debug("--- NEW ITERATION START ---")
                cfg: FullConfig = self.config_store.config
                now = time.time()
                
                found_delistings: Dict[str, str] = {}

                # 1. Дергаем публичные API: Timeline[3]
                timeline_rows = await self.symbols_api.get_asoon_delisting_symbols()
                self._debug_debug("API Timeline returned %d symbols", len(timeline_rows))
                for r in timeline_rows:
                    sym = self.normalize_symbol(r.symbol)
                    if sym not in found_delistings:
                        found_delistings[sym] = "API Timeline"

                # 2. Дергаем публичные API: Статус (дополнительный детектор)
                status_rows = await self.symbols_api.get_status_delisting_symbols()
                self._debug_debug("API Status returned %d symbols", len(status_rows))
                for r in status_rows:
                    sym = self.normalize_symbol(r.symbol)
                    if sym not in found_delistings:
                        found_delistings[sym] = "API Status"
                        
                # 3. Дергаем Email парсер
                if self.gmail_monitor and self.gmail_monitor.enabled:
                    email_symbols = await self.gmail_monitor.get_delisted_symbols()
                    self._debug_debug("Email Parser returned %d symbols: %s", len(email_symbols), email_symbols)
                    for s in email_symbols:
                        sym = self.normalize_symbol(s)
                        if sym not in found_delistings:
                            found_delistings[sym] = "Email Fallback"

                self.delisted_symbols = set(found_delistings.keys())
                self._debug_debug("Total Delisted Candidates (Union): %s", list(self.delisted_symbols))

                # Ищем новые монеты, которых раньше не было
                new_delisted = self.delisted_symbols - self.known_delisted_symbols
                if new_delisted:
                    self.known_delisted_symbols.update(new_delisted)

                # ОПТИМИЗАЦИЯ 1: Если нет ни одного делистинга вообще
                if not self.delisted_symbols:
                    self._debug_debug("No delistings found in public/email. Skipping private requests.")
                    self.detector.active_symbols = set()
                    self.tracked_matches = set()
                    await asyncio.sleep(cfg.app.game_loop_interval_sec)
                    continue
                    
                # ОПТИМИЗАЦИЯ 2: Если делистинги есть, но они старые (уже проверяли), И нет открытых позиций
                if not new_delisted and not self.active_alerts:
                    self._debug_debug("No NEW delistings and no active tracked positions. Skipping private requests.")
                    await asyncio.sleep(cfg.app.game_loop_interval_sec)
                    continue

                # 4. Если дошли сюда, значит либо появилась новая монета на делистинг, 
                # либо у нас висит открытая тревога, и надо следить, закрыл ли юзер позицию.
                active_raw = await self.private_client.get_active_symbols()
                self._debug_debug("Private API Active Positions returned %d symbols: %s", len(active_raw), active_raw)
                self.detector.active_symbols = {self.normalize_symbol(s) for s in active_raw}

                # 5. Изолированное сравнение множеств
                current_matches = self.detector.active_symbols.intersection(self.delisted_symbols)
                new_signals = current_matches - self.tracked_matches

                alerts_to_send: Dict[int, List[tuple[str, str]]] = defaultdict(list)

                for sym in current_matches:
                    source = found_delistings[sym]
                    if sym in new_signals:
                        self.active_alerts[sym] = {"repeat_num": 1, "last_sent": now, "source": source}
                        alerts_to_send[1].append((sym, source))
                    else:
                        alert = self.active_alerts.get(sym)
                        if alert and alert["repeat_num"] < cfg.delisting_repeats:
                            if now - alert["last_sent"] >= cfg.delisting_interval_sec:
                                alert["repeat_num"] += 1
                                alert["last_sent"] = now
                                alerts_to_send[alert["repeat_num"]].append((sym, source))

                for repeat_num, syms_data in alerts_to_send.items():
                    # Группируем по источнику
                    source_groups = defaultdict(list)
                    for sym, src in syms_data:
                        source_groups[src].append(sym)
                        
                    for src, syms in source_groups.items():
                        joined_symbols = ", ".join(syms)
                        await self._dispatch_alert(joined_symbols, src, repeat_num, cfg)

                closed_symbols = self.active_alerts.keys() - current_matches
                for sym in closed_symbols:
                    logger.info("Позиция по %s успешно закрыта на бирже. Удаляем из RAM кэша.", sym)
                    del self.active_alerts[sym]

                self.tracked_matches = current_matches
                self._debug_debug("--- ITERATION END. Current matches: %s ---", list(current_matches))

            except Exception as e:
                logger.error("Ошибка итерации гейм-лупы: %s", e)

            await asyncio.sleep(self.config_store.config.app.game_loop_interval_sec)

    async def _dispatch_alert(self, symbols_str: str, source: str, repeat_num: int, cfg: FullConfig) -> None:
        try:
            title = cfg.alert_title_template.format(symbols=symbols_str)
            body = cfg.alert_body_template.format(
                symbols=symbols_str, 
                source=source, 
                repeat_num=repeat_num, 
                total_repeats=cfg.delisting_repeats
            )
            
            await self.notifier_manager.send(title, body, priority=1)
            logger.warning("🚨 СИГНАЛ ОТПРАВЛЕН: %s [Источник: %s] [Повтор %d/%d]", symbols_str, source, repeat_num, cfg.delisting_repeats)
        except Exception as e:
            logger.error("Не удалось отправить нотификацию для %s: %s", symbols_str, e)



# async def run_game_loop(self) -> None:
#     while self.is_monitoring:
#         try:
#             cfg = self.config_store.config
#             now = time.time()
#             found_delistings: Dict[str, str] = {}

#             # Только email
#             if self.gmail_monitor and self.gmail_monitor.enabled:
#                 email_symbols = await self.gmail_monitor.get_delisted_symbols()
#                 for s in email_symbols:
#                     sym = self.normalize_symbol(s)
#                     found_delistings[sym] = "Email"

#             self.delisted_symbols = set(found_delistings.keys())

#             if not self.delisted_symbols:
#                 self.detector.active_symbols = set()
#                 self.active_alerts.clear()      # ← чистить, иначе зависнут старые
#                 self.tracked_matches = set()
#                 await asyncio.sleep(cfg.app.game_loop_interval_sec)
#                 continue

#             active_raw = await self.private_client.get_active_symbols()
#             self.detector.active_symbols = {self.normalize_symbol(s) for s in active_raw}

#             current_matches = self.detector.active_symbols & self.delisted_symbols
#             new_signals = current_matches - self.tracked_matches

#             alerts_to_send: Dict[int, List[str]] = defaultdict(list)
#             for sym in current_matches:
#                 if sym in new_signals:
#                     self.active_alerts[sym] = {"repeat_num": 1, "last_sent": now}
#                     alerts_to_send[1].append(sym)
#                 else:
#                     alert = self.active_alerts.get(sym)
#                     if alert and alert["repeat_num"] < cfg.delisting_repeats:
#                         if now - alert["last_sent"] >= cfg.delisting_interval_sec:
#                             alert["repeat_num"] += 1
#                             alert["last_sent"] = now
#                             alerts_to_send[alert["repeat_num"]].append(sym)

#             for repeat_num, syms in alerts_to_send.items():
#                 await self._dispatch_alert(", ".join(syms), repeat_num, cfg)

#             # Безопасное удаление — сначала собираем в set
#             for sym in set(self.active_alerts.keys()) - current_matches:
#                 del self.active_alerts[sym]

#             self.tracked_matches = current_matches

#         except Exception as e:
#             logger.error("Ошибка гейм-лупы: %s", e)

#         await asyncio.sleep(self.config_store.config.app.game_loop_interval_sec)