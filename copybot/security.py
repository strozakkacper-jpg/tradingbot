from cryptography.fernet import Fernet


class Vault:
    """Szyfruje klucze API użytkowników przed zapisem do bazy."""

    def __init__(self, key: str):
        self._f = Fernet(key.encode())

    def enc(self, value: str | None) -> str | None:
        return self._f.encrypt(value.encode()).decode() if value else None

    def dec(self, value: str | None) -> str | None:
        return self._f.decrypt(value.encode()).decode() if value else None
