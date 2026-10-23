"""Encryption at rest for event data and subscription secrets."""

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

_cipher: MultiFernet | None = None


def configure_cipher(keys: list[str]) -> None:
    global _cipher
    if not keys:
        raise ValueError("At least one data encryption key is required")
    _cipher = MultiFernet([Fernet(k.encode()) for k in keys])


def _get_cipher() -> MultiFernet:
    if _cipher is None:
        from app.config import get_settings

        configure_cipher(get_settings().encryption_keys)
    assert _cipher is not None
    return _cipher


def encrypt(value: str) -> str:
    return _get_cipher().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _get_cipher().decrypt(token.encode()).decode()


class EncryptedText(TypeDecorator[str]):
    """Text column transparently encrypted with Fernet (AES-128-CBC + HMAC)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        return None if value is None else encrypt(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        return None if value is None else decrypt(value)
