from __future__ import annotations

"""Secure storage helpers for sensitive strings (e.g. SQL password).

Strategy:
- On Windows: use DPAPI (CryptProtectData / CryptUnprotectData via ctypes).
  DPAPI binds the ciphertext to current Windows user account, so the encrypted
  blob is useless on another machine / under another user.
- On other OS or if DPAPI fails: fall back to base64 obfuscation. This is NOT
  real security, but avoids keeping plain-text passwords visible in config.ini
  and keeps the app functional outside Windows for development.

Encrypted values are stored as strings with a known prefix ("dpapi:" / "b64:")
so we can transparently decide how to decrypt them later.
"""

import base64
import ctypes
import ctypes.wintypes
import os
import sys

_DPAPI_PREFIX = "dpapi:"
_B64_PREFIX = "b64:"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _dpapi_protect(plain_bytes: bytes) -> bytes | None:
    if not _is_windows():
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
    except Exception:
        return None

    blob_in = _DataBlob(len(plain_bytes), ctypes.cast(
        ctypes.create_string_buffer(plain_bytes, len(plain_bytes)),
        ctypes.POINTER(ctypes.c_char),
    ))
    blob_out = _DataBlob()

    description = ctypes.c_wchar_p("CentralHub secret")
    success = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        description,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not success:
        return None

    try:
        data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return data
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(cipher_bytes: bytes) -> bytes | None:
    if not _is_windows():
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
    except Exception:
        return None

    blob_in = _DataBlob(len(cipher_bytes), ctypes.cast(
        ctypes.create_string_buffer(cipher_bytes, len(cipher_bytes)),
        ctypes.POINTER(ctypes.c_char),
    ))
    blob_out = _DataBlob()

    success = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not success:
        return None

    try:
        data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return data
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(blob_out.pbData)


def encrypt_secret(value: str) -> str:
    """Encode a secret string into a storable form.

    Returns empty string for empty input (so we do not write anything silly).
    """
    if not value:
        return ""
    plain_bytes = value.encode("utf-8")

    cipher = _dpapi_protect(plain_bytes)
    if cipher:
        return _DPAPI_PREFIX + base64.b64encode(cipher).decode("ascii")

    return _B64_PREFIX + base64.b64encode(plain_bytes).decode("ascii")


def decrypt_secret(stored_value: str) -> str:
    """Decode a previously stored secret.

    Accepts:
    - "dpapi:..." Windows-only, tied to current user.
    - "b64:..."   portable obfuscation (base64).
    - Any other non-empty value is treated as legacy plain-text password and
      returned as-is (so users who had plain passwords in config.ini keep
      working until they save settings again).
    """
    if not stored_value:
        return ""
    if stored_value.startswith(_DPAPI_PREFIX):
        try:
            raw = base64.b64decode(stored_value[len(_DPAPI_PREFIX) :].encode("ascii"))
        except Exception:
            return ""
        plain = _dpapi_unprotect(raw)
        if plain is None:
            return ""
        return plain.decode("utf-8", errors="replace")
    if stored_value.startswith(_B64_PREFIX):
        try:
            raw = base64.b64decode(stored_value[len(_B64_PREFIX) :].encode("ascii"))
        except Exception:
            return ""
        return raw.decode("utf-8", errors="replace")
    return stored_value


def looks_encrypted(stored_value: str) -> bool:
    if not stored_value:
        return False
    return stored_value.startswith(_DPAPI_PREFIX) or stored_value.startswith(_B64_PREFIX)
