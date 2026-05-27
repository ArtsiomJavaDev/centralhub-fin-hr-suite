"""Privacy-sensitive local lists used by import/export rules.

This module intentionally contains no real PESEL values.  Operational lists are
loaded from `_sensitive_lists.py`, which is git-ignored and must stay local to
the workstation.
"""
from __future__ import annotations

from collections.abc import Iterable


def _normalise_pesel_set(values: Iterable[object]) -> frozenset[str]:
    out: set[str] = set()
    for value in values:
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        if len(digits) == 11:
            out.add(digits)
    return frozenset(out)


try:
    import _sensitive_lists as _local_lists
except ImportError:
    _local_lists = None


UMOWY_EXPORT_EXCLUDE_PESELS = _normalise_pesel_set(
    getattr(_local_lists, "UMOWY_EXPORT_EXCLUDE_PESELS", ())
)

SPECIAL_CHOROBOWE_PESELS = _normalise_pesel_set(
    getattr(_local_lists, "SPECIAL_CHOROBOWE_PESELS", ())
)

