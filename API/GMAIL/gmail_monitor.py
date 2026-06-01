# ============================================================
# FILE: API/GMAIL/gmail_monitor.py
# ROLE: Монитор почты (IMAP) для резервного обнаружения делистингов.
# test: python -m API.GMAIL.gmail_monitor
# ============================================================

import imaplib
import email
from email.header import decode_header
import asyncio
import os
import re
from typing import List, Optional
from c_log import UnifiedLogger

logger = UnifiedLogger("gmail_monitor", spam_throttle=10.0)

class GmailMonitor:
    def __init__(self, all_known_symbols: List[str]):
        """
        all_known_symbols: Список известных биржевых символов (например, 'BTCUSDT', 'ETHUSDT') для вычленения из текста.
        """
        self.all_symbols = [s.upper() for s in all_known_symbols]
        try:
            from consts import _store
            config = _store.config.email_monitor
            self.enabled = config.enabled
            self.poll_interval = config.poll_interval_sec
            self.max_messages = config.max_messages
            self.imap_server = config.imap_server
            self.sender_filter = config.sender_filter
        except Exception as e:
            logger.error("Failed to load email config: %s", e)
            self.enabled = False
            self.poll_interval = 5

        self.user = os.getenv("GMAIL_USER", "").strip()
        self.password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
        
        if self.enabled and (not self.user or not self.password):
            logger.warning("Email monitor enabled in config, but GMAIL_USER or GMAIL_APP_PASSWORD not set in .env. Disabling fallback.")
            self.enabled = False
            
        self._last_sync_time = 0.0
        self._cached_symbols = []

    def _extract_symbols(self, text: str) -> List[str]:
        text_upper = text.upper()
        found = []
        
        # 1. Сначала ищем по точным маскам монет из нашего кэша
        for sym in self.all_symbols:
            if sym in text_upper:
                found.append(sym)
        
        # 2. Резервный поиск по маске, если вдруг символ не в кэше
        # Ищем паттерн XXXUSDT (до 10 символов базовой валюты)
        fallback_matches = re.findall(r'\b([A-Z0-9]{2,10}USDT)\b', text_upper)
        for m in fallback_matches:
            if m not in found:
                found.append(m)
                
        return list(set(found))

    def _sync_check_emails(self, override_max: Optional[int] = None) -> List[str]:
        if not self.enabled:
            return []
            
        delisted_symbols = []
        try:
            # Подключаемся к IMAP серверу
            mail = imaplib.IMAP4_SSL(self.imap_server)
            mail.login(self.user, self.password)
            mail.select("inbox")

            # Ищем последние письма от отправителя
            status, messages = mail.search(None, f'(FROM "{self.sender_filter}")')
            
            if status != "OK":
                mail.logout()
                return []

            msg_nums = messages[0].split()
            
            limit = override_max if override_max is not None else self.max_messages
            recent_nums = msg_nums[-limit:] if msg_nums else []
            
            if recent_nums:
                # ОПТИМИЗАЦИЯ: Батчинг (забираем все нужные письма одним запросом)
                fetch_ids = b",".join(recent_nums).decode('ascii')
                res, msg_data = mail.fetch(fetch_ids, '(RFC822)')
                
                if res == "OK":
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])
                            subject_header = msg.get("Subject", "")
                            if not subject_header:
                                continue
                                
                            decoded_list = decode_header(subject_header)
                            subject, encoding = decoded_list[0]
                            if isinstance(subject, bytes):
                                subject = subject.decode(encoding or "utf-8", errors="ignore")
                                
                            # Читаем тело
                            body = ""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    content_type = part.get_content_type()
                                    if content_type == "text/plain":
                                        try:
                                            body += part.get_payload(decode=True).decode(errors="ignore")
                                        except Exception:
                                            pass
                            else:
                                try:
                                    body = msg.get_payload(decode=True).decode(errors="ignore")
                                except Exception:
                                    pass
                                    
                            full_text = f"{subject} {body}".lower()
                            
                            # Ищем ключевое слово делистинга
                            if "delist" in full_text:
                                # Извлекаем монеты
                                symbols = self._extract_symbols(f"{subject} {body}")
                                for s in symbols:
                                    if s not in delisted_symbols:
                                        delisted_symbols.append(s)

            mail.logout()
            
        except Exception as e:
            logger.error("Gmail sync error: %s", e)
            
        return delisted_symbols

    async def get_delisted_symbols(self, override_max: Optional[int] = None) -> List[str]:
        """Асинхронная обертка для проверки почты."""
        if not self.enabled:
            return []
            
        import time
        now = time.time()
        # Троттлинг: не спамим IMAP сервер чаще, чем poll_interval
        if override_max is None and (now - self._last_sync_time) < self.poll_interval:
            return self._cached_symbols
            
        symbols = await asyncio.to_thread(self._sync_check_emails, override_max)
        
        if override_max is None:
            self._cached_symbols = symbols
            self._last_sync_time = now
            
        return symbols

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    async def test():
        logger.info("🚀 ТЕСТОВЫЙ ЗАПУСК: GMAIL MONITOR")
        # Для теста добавим несколько фейковых и реальных символов
        test_symbols = ["BTCUSDT", "ETHUSDT", "MEUSDT", "VANRYUSDT", "HOOKUSDT"]
        
        monitor = GmailMonitor(all_known_symbols=test_symbols)
        
        if not monitor.user or not monitor.password:
            logger.error("Для теста необходимо указать GMAIL_USER и GMAIL_APP_PASSWORD в .env!")
            return
            
        # Принудительно включаем для теста
        monitor.enabled = True
        
        limit_str = input("Введите количество последних писем от Phemex для сканирования (например, 50): ").strip()
        # limit = int(limit_str) if limit_str.isdigit() else 10
        limit = 100
        
        logger.info(f"Начинаем сканирование {limit} последних писем от '{monitor.sender_filter}'...")
        
        start_t = asyncio.get_event_loop().time()
        found = await monitor.get_delisted_symbols(override_max=limit)
        end_t = asyncio.get_event_loop().time()
        
        logger.info(f"✅ Сканирование завершено за {end_t - start_t:.2f} сек.")
        if found:
            logger.info(f"🚨 НАЙДЕНЫ СИГНАЛЫ ДЕЛИСТИНГА: {found}")
        else:
            logger.info("Сигналов делистинга в проверенных письмах не обнаружено.")
            
    import sys
    asyncio.run(test())
