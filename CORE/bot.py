# File: CORE/bot.py
# Role: Bot orchestrator, game loop, specification loop, alert dispatcher.

import os
import sys
import json
import random
import asyncio
import aiohttp
from pathlib import Path
from typing import Dict, Set, Any, Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

# Add workspace directory to python path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

import consts
from consts import ConfigStore
from c_log import UnifiedLogger
from API.PHEMEX.get_pos_symbols import PhemexPrivateRESTFallback
from API.PHEMEX.order import PhemexPrivateClient
from API.PHEMEX.symbol import PhemexSymbols, SymbolInfo
from API.PHEMEX.ticker import PhemexTickerAPI
from API.TG.notifier import TelegramNotifier, NotificationManager
from API.pushover.pushover import PushoverNotifier
from API.pushover.alertzy import AlertzyNotifier
from API.pushover.techulus import TechulusPushNotifier

logger = UnifiedLogger("bot")

ERROR_CODE_MAP = {
    11011: "TE_REDUCE_ONLY_ABORT",
    11057: "TE_QTY_NOT_MATCH_REDUCE_ONLY"
}

class OrderMetricsCalculator:
    """
    Класс для изолированного расчета параметров лимитного ордера проверки.
    Вычисляет цену, объем и стороны ордера на основе конфигурации и рыночных правил.
    """
    @staticmethod
    def calculate(
        symbol: str, 
        current_price: float, 
        tick_size: float, 
        lot_size: float, 
        side_cfg: str, 
        size_cfg: float, 
        dist_pct: float
    ) -> Dict[str, Any]:
        if side_cfg.upper() == "LONG":
            order_side = "Buy"
            pos_side = "Long"
            target_price = current_price * (1.0 - dist_pct / 100.0)
        else:
            order_side = "Sell"
            pos_side = "Short"
            target_price = current_price * (1.0 + dist_pct / 100.0)

        # Округляем цену до tick_size
        target_price = round(target_price / tick_size) * tick_size

        # Округляем объем до lot_size
        qty = size_cfg / target_price
        qty = round(qty / lot_size) * lot_size
        if qty < lot_size:
            qty = lot_size

        return {
            "side": order_side,
            "pos_side": pos_side,
            "price": target_price,
            "qty": qty
        }


class DelistingDetectorOrchestrator:
    """
    Основной управляющий класс робота (Оркестратор).
    Отвечает за инициализацию, фоновые циклы и обработку сигналов.
    """
    def __init__(self):
        self.config_store: Optional[ConfigStore] = None
        self.is_monitoring = False
        self.delisted_symbols: Set[str] = set()
        self.active_alerts: Dict[str, asyncio.Task] = {}
        
        self.private_client: Optional[PhemexPrivateRESTFallback] = None
        self.donor_client: Optional[PhemexPrivateClient] = None
        self.symbols_api: Optional[PhemexSymbols] = None
        self.ticker_api: Optional[PhemexTickerAPI] = None
        self.notifier_manager: Optional[NotificationManager] = None
        
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self.session: Optional[aiohttp.ClientSession] = None
        
        self.active_symbols: Set[str] = set()
        self.symbol_specs: Dict[str, SymbolInfo] = {}
        self.symbol_prices: Dict[str, float] = {}
        
        self.game_loop_task: Optional[asyncio.Task] = None
        self.spec_loop_task: Optional[asyncio.Task] = None
        self.price_loop_task: Optional[asyncio.Task] = None
        
        # Для совместимости с telegram_bot.py: ctx.detector.active_symbols
        self.detector = self
        
        # Константы параметров риска (загружаются один раз при старте)
        self.risk_side = "LONG"
        self.risk_size = 7.0
        self.risk_dist_pct = 25.0
        
        # Состояния готовности данных из фоновых потоков
        self.specs_ready = False
        self.prices_ready = False

    def load_delisted_symbols(self):
        path = BASE_DIR / "last_delist_coins.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.delisted_symbols = set(data)
                        logger.info("Loaded %d previously delisted coins from last_delist_coins.json", len(self.delisted_symbols))
            except Exception as e:
                logger.error("Failed to load last_delist_coins.json: %s", e)

    def save_delisted_symbols(self):
        path = BASE_DIR / "last_delist_coins.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(list(self.delisted_symbols), f, indent=4)
            logger.info("Saved %d delisted coins to last_delist_coins.json", len(self.delisted_symbols))
        except Exception as e:
            logger.error("Failed to save last_delist_coins.json: %s", e)

    async def update_specs(self) -> bool:
        """Синхронное получение спецификаций торговых инструментов."""
        try:
            specs = await self.symbols_api.get_all()
            self.symbol_specs = {s.symbol: s for s in specs}
            logger.info("Successfully fetched %d symbol specs", len(self.symbol_specs))
            return True
        except Exception as e:
            logger.error("Failed to fetch symbol specs: %s", e)
            return False

    async def update_prices(self) -> bool:
        """Синхронное получение текущих рыночных цен."""
        try:
            prices = await self.ticker_api.get_all_prices()
            self.symbol_prices = prices
            return True
        except Exception as e:
            logger.error("Failed to fetch ticker prices: %s", e)
            return False

    async def start(self):
        if self.is_monitoring:
            logger.warning("Monitoring services already running.")
            return

        logger.info("Starting monitoring initialization...")
        
        # Загружаем константы риска один раз из конфигурации
        risk_cfg = self.config_store.config.raw.get("risk", {})
        self.risk_side = str(risk_cfg.get("side", "LONG"))
        self.risk_size = float(risk_cfg.get("size", 7.0))
        self.risk_dist_pct = float(risk_cfg.get("limite_distance_pct", 25.0))
        logger.info("Risk constants loaded: side=%s, size=%s, distance=%s%%", 
                    self.risk_side, self.risk_size, self.risk_dist_pct)

        # Сбрасываем флаги готовности данных перед запуском
        self.specs_ready = False
        self.prices_ready = False
        self.is_monitoring = True
        
        # Запускаем фоновые циклы обновления спецификаций и цен сразу
        self.spec_loop_task = asyncio.create_task(self.spec_loop())
        self.price_loop_task = asyncio.create_task(self.price_loop())
        self.game_loop_task = asyncio.create_task(self.game_loop())
        logger.info("Monitoring services successfully started.")

    async def stop(self):
        if not self.is_monitoring:
            logger.warning("Monitoring services already stopped.")
            return
        
        self.is_monitoring = False
        
        if self.game_loop_task:
            self.game_loop_task.cancel()
            self.game_loop_task = None
            
        if self.spec_loop_task:
            self.spec_loop_task.cancel()
            self.spec_loop_task = None
            
        if self.price_loop_task:
            self.price_loop_task.cancel()
            self.price_loop_task = None
            
        # Отменяем фоновые задачи отправки алертов
        for symbol, task in list(self.active_alerts.items()):
            task.cancel()
        self.active_alerts.clear()
        
        logger.info("Monitoring services stopped.")

    async def spec_loop(self):
        """Редкий цикл обновления правил спецификаций."""
        while self.is_monitoring:
            try:
                if await self.update_specs():
                    self.specs_ready = True
                cfg = self.config_store.config.app
                await asyncio.sleep(cfg.spec_loop_interval_sec)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in spec_loop: %s", e)
                await asyncio.sleep(5.0)

    async def price_loop(self):
        """Высокочастотный цикл обновления цен для точности расчетов (каждые 1-2 сек)."""
        while self.is_monitoring:
            try:
                if await self.update_prices():
                    self.prices_ready = True
                cfg = self.config_store.config.app
                await asyncio.sleep(cfg.price_update_interval_sec)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in price_loop: %s", e)
                await asyncio.sleep(1.0)

    async def game_loop(self):
        """Основной цикл опроса баланса клиента и постановки ордеров на донор."""
        # Ждем первого валидного ответа от spec_loop и price_loop
        while self.is_monitoring and not (self.specs_ready and self.prices_ready):
            logger.info("Waiting for symbol specs and prices initialization (specs=%s, prices=%s)...",
                        self.specs_ready, self.prices_ready)
            await asyncio.sleep(1.0)
            
        while self.is_monitoring:
            cfg = self.config_store.config.app
            interval_cfg = cfg.game_loop_interval_sec
            if isinstance(interval_cfg, list):
                sleep_time = random.uniform(interval_cfg[0], interval_cfg[1])
            else:
                sleep_time = float(interval_cfg)
                
            logger.info("Game loop sleeping for %.2f seconds...", sleep_time)
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break

            try:
                logger.info("🔎 Querying active positions/orders using single-request API...")
                # Получаем активные монеты клиента через единый метод фильтрации
                active_symbols = await self.private_client.get_active_symbols()
                self.active_symbols = active_symbols
                logger.info("Found %d active symbols on client account: %s", len(active_symbols), list(active_symbols))
                
                for symbol in active_symbols:
                    if symbol in self.delisted_symbols:
                        continue
                    
                    sym_info = self.symbol_specs.get(symbol)
                    price = self.symbol_prices.get(symbol)
                    
                    if not sym_info or not price:
                        logger.warning("Missing specification or price for %s, skipping donor check this turn.", symbol)
                        continue
                    
                    await self.check_symbol_on_donor(symbol, sym_info, price)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in game_loop iteration: %s", e)

    async def check_symbol_on_donor(self, symbol: str, sym_info: SymbolInfo, current_price: float):
        logger.info("Placing verification order for %s on donor account...", symbol)
        try:
            # Вычисляем параметры через OrderMetricsCalculator с использованием констант класса
            metrics = OrderMetricsCalculator.calculate(
                symbol=symbol,
                current_price=current_price,
                tick_size=sym_info.tick_size,
                lot_size=sym_info.lot_size,
                side_cfg=self.risk_side,
                size_cfg=self.risk_size,
                dist_pct=self.risk_dist_pct
            )
            
            # Отправляем запрос на донорский аккаунт
            resp = await self.donor_client.place_limit_order(
                symbol=symbol,
                side=metrics["side"],
                qty=metrics["qty"],
                price=metrics["price"],
                pos_side=metrics["pos_side"]
            )
            
            code = resp.get("code", -1)
            msg = str(resp.get("msg", ""))
            
            reduce_only_codes = {11011, 11057}
            is_reduce_only = (code in reduce_only_codes) or ("REDUCE_ONLY" in msg.upper())
            
            if is_reduce_only:
                logger.warning("🚨 [REDUCE_ONLY] Coin %s is in reduce-only/delisting mode (Code: %s, Msg: %s)", symbol, code, msg)
                self.delisted_symbols.add(symbol)
                self.save_delisted_symbols()
                
                if symbol not in self.active_alerts:
                    self.active_alerts[symbol] = asyncio.create_task(self.run_repeating_alerts(symbol, code, msg))
            elif code == 0:
                order_id = resp.get("data", {}).get("orderID")
                logger.info("Verification order placed successfully on donor account for %s. ID: %s. Scheduling cancel...", symbol, order_id)
                
                # Задержка отмены ордера берется строго из конфига AppConfig
                app_cfg = self.config_store.config.app
                asyncio.create_task(self.cancel_donor_order_after_delay(symbol, order_id, metrics["pos_side"], app_cfg.cancel_delay_sec))
            else:
                logger.error("Verification order failed for %s with code %s: %s", symbol, code, msg)
                
        except Exception as e:
            logger.error("Error performing donor check for %s: %s", symbol, e)

    async def cancel_donor_order_after_delay(self, symbol: str, order_id: str, pos_side: str, delay: float):
        try:
            await asyncio.sleep(delay)
            logger.info("Cancelling verification order %s for %s...", order_id, symbol)
            cancel_resp = await self.donor_client.cancel_order(symbol, order_id, pos_side)
            code = cancel_resp.get("code", -1)
            if code == 0:
                logger.info("Successfully cancelled verification order %s for %s.", order_id, symbol)
            else:
                logger.error("Failed to cancel order %s for %s: %s", order_id, symbol, cancel_resp.get("msg", ""))
        except Exception as e:
            logger.error("Exception while cancelling order %s for %s: %s", order_id, symbol, e)

    async def run_repeating_alerts(self, symbol: str, error_code: int, error_msg: str):
        try:
            cfg = self.config_store.config
            total_repeats = cfg.delisting_repeats
            interval = cfg.delisting_interval_sec
            title_tmpl = cfg.alert_title_template
            body_tmpl = cfg.alert_body_template
            
            for repeat_num in range(1, total_repeats + 1):
                if not self.is_monitoring:
                    break
                    
                # Если клиент убрал ордера/позиции, останавливаем алерты
                if symbol not in self.active_symbols:
                    logger.info("Stopping alerts for %s: symbol is no longer active on client account.", symbol)
                    break
                    
                title = title_tmpl.format(symbols=symbol, symbol=symbol)
                
                # Добавляем краткие оригинальные детали ошибки биржи для компактности уведомлений
                desc = ERROR_CODE_MAP.get(error_code, "REDUCE_ONLY")
                details = f"\n\nError: {error_code} ({desc})"
                body = body_tmpl.format(symbols=symbol, symbol=symbol, repeat_num=repeat_num, total_repeats=total_repeats) + details
                
                logger.info("Sending delisting alert for %s (repeat %d of %d)...", symbol, repeat_num, total_repeats)
                await self.notifier_manager.send(title, body, priority=2)
                
                if repeat_num < total_repeats:
                    await asyncio.sleep(interval)
                    
        except asyncio.CancelledError:
            logger.info("Alert loop for %s cancelled.", symbol)
        except Exception as e:
            logger.error("Error in alert loop for %s: %s", symbol, e)
        finally:
            self.active_alerts.pop(symbol, None)

    async def start_app(self):
        load_dotenv()
        
        # Загружаем конфигурацию
        self.config_store = ConfigStore(BASE_DIR / "CONFIG" / "prod")
        
        # Подгружаем сохраненные делистинги
        self.load_delisted_symbols()
        
        # Общая сессия
        self.session = aiohttp.ClientSession()
        
        # Клиенты API
        prod_key = os.getenv("PROD_PHEMEX_API_KEY", "")
        prod_secret = os.getenv("PROD_PHEMEX_API_SECRET", "")
        donor_key = os.getenv("DONOR_PHEMEX_API_KEY", "")
        donor_secret = os.getenv("DONOR_PHEMEX_API_SECRET", "")
        
        self.private_client = PhemexPrivateRESTFallback(prod_key, prod_secret, self.session)
        self.donor_client = PhemexPrivateClient(donor_key, donor_secret, self.session)
        self.symbols_api = PhemexSymbols()
        self.ticker_api = PhemexTickerAPI()
        
        # Инициализация уведомлений
        tg_token = os.getenv("TG_BOT_TOKEN", "")
        allowed_user_ids = self.config_store.config.telegram.allowed_user_ids
        tg_enabled = self.config_store.config.telegram_alerts.enabled
        
        self.bot = Bot(token=tg_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        
        tg_notifier = TelegramNotifier(self.bot, allowed_user_ids, tg_enabled)
        
        po_token = os.getenv("ANDROID_PUSHOVER_TOKEN", "")
        po_user = os.getenv("ANDROID_PUSHOVER_USER", "")
        po_enabled = self.config_store.config.notifier_android.enabled
        po_notifier = PushoverNotifier(self.session, po_token, po_user, po_enabled)
        
        al_key = os.getenv("APPLE2_ALERTZY_KEY", "")
        al_enabled = self.config_store.config.notifier_apple2.enabled
        al_notifier = AlertzyNotifier(self.session, al_key, al_enabled)
        
        te_key = os.getenv("APPLE_NOTIFIER_KEY", "")
        te_enabled = self.config_store.config.notifier_apple.enabled
        te_notifier = TechulusPushNotifier(self.session, te_key, te_enabled)
        
        self.notifier_manager = NotificationManager([
            tg_notifier,
            po_notifier,
            al_notifier,
            te_notifier
        ])
        
        # Настройка хендлеров TG
        from API.TG.telegram_bot import register_handlers
        register_handlers(self.dp)
        
        logger.info("Initializing Telegram Bot UI polling...")
        asyncio.create_task(self.dp.start_polling(self.bot))
        
        # Запускаем мониторинг
        await self.start()
        
        logger.info("Application started. Waiting forever...")
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()
            await self.symbols_api.aclose()
            await self.ticker_api.aclose()
            await self.session.close()
            await self.bot.session.close()


# Экспортируем глобальный экземпляр класса для интеграции с TG ботом
ctx = DelistingDetectorOrchestrator()

async def start_monitoring_services():
    await ctx.start()

async def stop_monitoring_services():
    await ctx.stop()

async def start_app():
    await ctx.start_app()

if __name__ == "__main__":
    asyncio.run(start_app())
