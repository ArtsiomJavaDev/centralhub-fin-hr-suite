"""Shared PESEL and date utilities.

Centralises the logic that was previously duplicated between main.py and
db/service.py so that any fix or change only needs to happen once.

Public API
----------
normalize_pesel(value)          → str  (11-digit string, zero-padded)
birthdate_from_pesel(pesel)     → Optional[date]
age_on(birth, on_date)          → int
is_under_26(pesel, on_date)     → bool
is_female_from_pesel(pesel)     → Optional[bool]
first_day_of_next_month(value)  → date
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional


def normalize_pesel(value: object) -> str:
    """Return a normalised 11-digit PESEL string.

    Handles: int/float (e.g. read from Excel as numeric), str with non-digit
    characters, None / NaN.  Zero-pads short strings to 11 digits.
    Returns an empty string when the input cannot be interpreted.
    """
    if value is None:
        return ""
    # Avoid pandas import at module level — only check for NaN when necessary.
    try:
        import pandas as pd  # noqa: PLC0415
        if pd.isna(value):
            return ""
    except (TypeError, ValueError, ImportError):
        pass

    if isinstance(value, float) and value == int(value):
        digits = str(int(value))
    elif isinstance(value, int) and not isinstance(value, bool):
        digits = str(value)
    else:
        digits = re.sub(r"\D+", "", str(value))

    if digits and len(digits) < 11:
        digits = digits.zfill(11)
    return digits


def birthdate_from_pesel(pesel: object) -> Optional[date]:
    """Decode the birth date encoded in a Polish PESEL number.

    Century is encoded via the month field:
      01-12 → 1900-1999
      21-32 → 2000-2099
      41-52 → 2100-2199
      61-72 → 2200-2299
      81-92 → 1800-1899
    """
    digits = "".join(ch for ch in normalize_pesel(pesel) if ch.isdigit())
    if len(digits) < 6:
        return None
    try:
        yy = int(digits[0:2])
        mm = int(digits[2:4])
        dd = int(digits[4:6])
    except ValueError:
        return None

    if 1 <= mm <= 12:
        year = 1900 + yy
    elif 21 <= mm <= 32:
        year = 2000 + yy
        mm -= 20
    elif 41 <= mm <= 52:
        year = 2100 + yy
        mm -= 40
    elif 61 <= mm <= 72:
        year = 2200 + yy
        mm -= 60
    elif 81 <= mm <= 92:
        year = 1800 + yy
        mm -= 80
    else:
        return None

    try:
        return date(year, mm, dd)
    except ValueError:
        return None


def age_on(birth: date, on_date: date) -> int:
    """Return age in whole years on *on_date*."""
    years = on_date.year - birth.year
    if (on_date.month, on_date.day) < (birth.month, birth.day):
        years -= 1
    return years


def is_under_26(pesel: object, on_date: date) -> bool:
    """Return True when the PESEL holder is strictly under 26 on *on_date*."""
    birth = birthdate_from_pesel(pesel)
    if birth is None:
        return False
    return age_on(birth, on_date) < 26


def is_female_from_pesel(pesel: object) -> Optional[bool]:
    """Return True for female, False for male, None when PESEL is invalid.

    PESEL rule: the 10th digit (index 9) is even → female, odd → male.
    """
    digits = "".join(ch for ch in normalize_pesel(pesel) if ch.isdigit())
    if len(digits) < 10:
        return None
    try:
        sex_digit = int(digits[9])
    except ValueError:
        return None
    return (sex_digit % 2) == 0


def first_day_of_next_month(value: date) -> date:
    """Return the first day of the month following *value*."""
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)
