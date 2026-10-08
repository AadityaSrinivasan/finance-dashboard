"""Encrypts Plaid access tokens before they are written to the database.

The encryption key lives in Windows Credential Manager (protected by your
Windows login), not in this folder. So a copy of the database file alone,
or even this whole folder, cannot be used to read your accounts.
Use scripts/backup_key.py to save a copy of the key somewhere safe."""
import keyring
from cryptography.fernet import Fernet, InvalidToken

SERVICE = "finance-dashboard"
KEY_NAME = "plaid-token-key"
_fernet = None


class TokenKeyMissing(RuntimeError):
    pass


def _get_fernet(create: bool = True) -> Fernet:
    global _fernet
    if _fernet is None:
        key = keyring.get_password(SERVICE, KEY_NAME)
        if not key:
            if not create:
                raise TokenKeyMissing("Token encryption key not found in Windows Credential Manager.")
            key = Fernet.generate_key().decode()
            keyring.set_password(SERVICE, KEY_NAME, key)
        _fernet = Fernet(key.encode())
    return _fernet


def encrypt(token: str) -> str:
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt(blob: str) -> str:
    try:
        return _get_fernet(create=False).decrypt(blob.encode()).decode()
    except InvalidToken:
        raise TokenKeyMissing("Token encryption key does not match the stored tokens. "
                              "Restore it with scripts/restore_key.py.")


def export_key() -> str:
    key = keyring.get_password(SERVICE, KEY_NAME)
    if not key:
        raise TokenKeyMissing("No token encryption key exists yet.")
    return key


def import_key(key: str):
    global _fernet
    Fernet(key.encode())  # validate format
    keyring.set_password(SERVICE, KEY_NAME, key)
    _fernet = None
