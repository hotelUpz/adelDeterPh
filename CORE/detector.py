# File: CORE/detector.py
# Role: Фоновый интерпретатор приватного WebSocket канала (aop_p).
#       Определяет активные позиции по трём критериям:
#       открытые позиции, лимитные ордера, триггерные/условные ордера.
#       Чистый реактивный компонент — никаких циклов, никаких REST-запросов.
from __future__ import annotations

from typing import Dict, Set
from c_log import UnifiedLogger

logger = UnifiedLogger("detector", spam_throttle=1.0)


class DelistingDetector:
    """Интерпретатор приватного WS-потока. Отслеживает активные позиции и ордера."""

    def __init__(self):
        # Позиции (symbol set)
        self.ws_active_positions: Set[str] = set()
        # Ордера (order_id -> symbol)
        self.ws_active_orders: Dict[str, str] = {}
        # Итоговый набор активных символов (union позиций и ордеров)
        self.active_symbols: Set[str] = set()

    def process_ws_message(self, msg: dict):
        """Интерпретатор сообщений приватного WebSocket канала (aop_p).
        
        Вызывается синхронно из оркестратора при получении WS-сообщения.
        Обновляет внутренние множества позиций и ордеров.
        """
        try:
            positions = msg.get("positions") or []
            orders = msg.get("orders") or []

            # Обработка позиций
            for pos in positions:
                symbol = pos.get("symbol")
                if not symbol:
                    continue
                side = pos.get("side", "None")
                size = float(pos.get("size", 0) or pos.get("sizeRv", 0) or 0)
                if side != "None" and size > 0:
                    self.ws_active_positions.add(symbol)
                else:
                    self.ws_active_positions.discard(symbol)

            # Обработка ордеров (лимитные, триггерные)
            for ord_data in orders:
                symbol = ord_data.get("symbol")
                order_id = ord_data.get("orderID")
                if not symbol or not order_id:
                    continue
                status = ord_data.get("action") or ord_data.get("ordStatus") or ""
                if status in ("New", "PartiallyFilled", "Untriggered", "TriggeredPending"):
                    self.ws_active_orders[order_id] = symbol
                else:
                    self.ws_active_orders.pop(order_id, None)

            self.active_symbols = self.ws_active_positions.union(self.ws_active_orders.values())
        except Exception as e:
            logger.error("Error processing WS message: %s", e)
