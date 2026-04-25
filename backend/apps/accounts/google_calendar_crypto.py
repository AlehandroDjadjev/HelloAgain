from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger(__name__)

_TOKEN_PREFIX = "enc:"


def _fernet() -> Fernet:
    secret = (
        str(os.environ.get("GOOGLE_TOKEN_ENCRYPTION_KEY") or "").strip()
        or str(getattr(settings, "SECRET_KEY", "") or "").strip()
    )
    if not secret:
        raise RuntimeError("Missing token encryption secret.")
    derived_key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(derived_key)


def encrypt_token(raw_value: str) -> str:
    clean = str(raw_value or "").strip()
    if not clean:
        return ""
    encrypted = _fernet().encrypt(clean.encode("utf-8")).decode("utf-8")
    return f"{_TOKEN_PREFIX}{encrypted}"


def decrypt_token(stored_value: str) -> str:
    raw = str(stored_value or "").strip()
    if not raw:
        return ""
    if not raw.startswith(_TOKEN_PREFIX):
        # Backward compatibility for earlier plaintext development data.
        return raw
    try:
        return _fernet().decrypt(raw[len(_TOKEN_PREFIX) :].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.warning("google_calendar.decrypt_failed invalid_token")
        return ""
