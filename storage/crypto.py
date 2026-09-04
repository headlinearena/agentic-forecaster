import os

from cryptography.fernet import Fernet


class CryptoError(RuntimeError):
    pass


def _get_fernet() -> Fernet:
    key = os.environ.get("AGENT_DB_ENCRYPTION_KEY")
    if not key:
        raise CryptoError("Missing required env var: AGENT_DB_ENCRYPTION_KEY")
    return Fernet(key.encode("utf-8"))


def encrypt_secret(plaintext: str) -> bytes:
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_secret(ciphertext: bytes) -> str:
    return _get_fernet().decrypt(bytes(ciphertext)).decode("utf-8")
