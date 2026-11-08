"""Windows DPAPI encrypted credentials stored *inside* the PunchCard project.

No use of Windows Credential Manager. The encrypted file itself is deleted when
project/data is deleted. DPAPI protects secrets for the current Windows user;
copying credentials.dpapi to a different Windows account is not supported.
"""
import json
import os
import tempfile
import threading
from pathlib import Path

_LOCK = threading.RLock()


def _protect(plaintext: bytes) -> bytes:
    try:
        import win32crypt
    except ImportError as exc:
        raise RuntimeError('PunchCard credential encryption requires Windows and pywin32') from exc
    return win32crypt.CryptProtectData(plaintext, 'PunchCard API credentials', None, None, None, 0)


def _unprotect(ciphertext: bytes) -> bytes:
    try:
        import win32crypt
    except ImportError as exc:
        raise RuntimeError('PunchCard credential decryption requires Windows and pywin32') from exc
    try:
        return win32crypt.CryptUnprotectData(ciphertext, None, None, None, 0)[1]
    except Exception as exc:
        raise RuntimeError('Cannot decrypt project/data/credentials.dpapi for this Windows user. '
                           'Use the same Windows account or remove the file and re-enter the tokens.') from exc


class CredentialVault:
    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def _key(service: str, account: str) -> str:
        return f'{service}\u0000{account}'

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        data = json.loads(_unprotect(self.path.read_bytes()).decode('utf-8'))
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('secrets'), dict):
            raise RuntimeError('Unrecognised PunchCard credential file format')
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in data['secrets'].items()):
            raise RuntimeError('Invalid PunchCard credential records')
        return data['secrets']

    def get_password(self, service: str, account: str) -> str | None:
        with _LOCK:
            return self._read().get(self._key(service, account))

    def set_password(self, service: str, account: str, token: str) -> None:
        if not token:
            raise ValueError('Cannot store an empty token')
        with _LOCK:
            secrets = self._read()
            secrets[self._key(service, account)] = token
            self._write(secrets)

    def delete_password(self, service: str, account: str) -> None:
        with _LOCK:
            secrets = self._read()
            if secrets.pop(self._key(service, account), None) is not None:
                self._write(secrets)

    def _write(self, secrets: dict[str, str]):
        plaintext = json.dumps({'version': 1, 'secrets': secrets}, separators=(',', ':')).encode('utf-8')
        ciphertext = _protect(plaintext)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='wb', prefix='.credentials-', suffix='.tmp',
                                             dir=self.path.parent, delete=False) as tmp:
                temp_path = Path(tmp.name)
                tmp.write(ciphertext)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
