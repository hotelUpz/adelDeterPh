# File: consts.py
# Role: Configuration management, global constants, and legacy bridge.
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytz

BASE_DIR = Path(__file__).resolve().parent

# Default global variables (fallbacks)
CURRENCY = "USDT"
LOG_DEBUG = True
LOG_ERROR = True
LOG_INFO = True
LOG_WARNING = True
MAX_LOG_LINES = 100
MAX_ALL_LOGS_LINES = 100000
TIME_ZONE = "UTC"
TZ = pytz.timezone(TIME_ZONE)
SPAM_THROTTLE_SEC = 60.0
