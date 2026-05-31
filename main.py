# File: main.py
import asyncio
from CORE.bot import start_app

if __name__ == "__main__":
    try:
        asyncio.run(start_app())
    except KeyboardInterrupt:
        print("\nProcess terminated by user.")
