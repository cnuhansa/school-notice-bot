"""Shared test setup: import path and date helpers."""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def days_out(n: int) -> str:
    """An ISO date `n` days from today — deadlines must stay relative."""
    return (date.today() + timedelta(days=n)).isoformat()
