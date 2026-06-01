import asyncio
import json
import urllib.request
import sys
import os

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("❌ Библиотека playwright не найдена.")
    print("Для работы этой утилиты выполните в терминале:")
    print("pip install playwright")
    print("playwright install chromium")
    sys.exit(1)

def fetch_delisted_perps():
    print("⏳ Загрузка списка рынков с Phemex API...")
    url = 'https://api.phemex.com/public/products'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode('utf-8'))
        
    products = data.get('data', {}).get('perpProductsV2', [])
    delisted = [p for p in products if p.get('status') == 'Delisted']
    delisted.sort(key=lambda x: x.get('listTime', 0), reverse=True)
    return delisted

async def open_and_close_tab(context, url: str, index: int, total: int):
    try:
        page = await context.new_page()
        print(f"✅ [{index}/{total}] ОТКРЫТ: {url}")
        # Пытаемся перейти по ссылке, но не ждем полной загрузки если она тупит
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # Висит ровно 60 секунд
        await asyncio.sleep(60)
        
        await page.close()
        print(f"❌ [{index}/{total}] ЗАКРЫТ (прошла 1 минута): {url}")
    except Exception as e:
        print(f"⚠️ [{index}/{total}] Ошибка на {url}: {e}")

async def main():
    delisted = fetch_delisted_perps()
    total = len(delisted)
    print(f"✅ Найдено делистнутых USDT-перпов: {total} шт.")
    
    if total == 0:
        return

    print("\n🚀 Запускаю браузер... Вкладки будут открываться раз в 1 секунду и закрываться через 60 секунд.")
    
    async with async_playwright() as p:
        # Запускаем Chromium с интерфейсом (не headless), чтобы вы могли смотреть
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={'width': 1280, 'height': 800})
        
        tasks = []
        count = 0
        
        for p_data in delisted:
            symbol = p_data.get('symbol', '')
            if symbol.endswith("USDT"):
                count += 1
                base = symbol[:-4]
                url = f"https://phemex.com/ru/futures/{base}-USDT"
                
                # Создаем независимую задачу для каждой вкладки
                task = asyncio.create_task(open_and_close_tab(context, url, count, total))
                tasks.append(task)
                
                # Ждем 1 секунду перед открытием следующей
                await asyncio.sleep(1.0)
                
        # Ждем пока все вкладки не закроются сами (последняя закроется через 60 сек после открытия)
        await asyncio.gather(*tasks)
        await browser.close()
        print("\n✅ Все монеты проверены, браузер закрыт.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Остановлено пользователем.")
