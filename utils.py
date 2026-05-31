# File: utils.py
# Role: Helper utilities.
from decimal import Decimal

def float_to_str(value: float) -> str:
    return f"{Decimal(str(value)):f}"
