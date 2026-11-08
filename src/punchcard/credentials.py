"""Store PunchCard API tokens as Windows Credential Manager generic credentials.

Each Jira account and the Tempo API token has its own Windows credential.
No tokens are written to PunchCard files. A credential belongs to the current
Windows user; users can inspect or remove it in Windows Credential Manager.
"""

import threading


class CredentialVault:
    """Thin adapter over Windows Credential Manager for API tokens."""

    def __init__(self):
        self._lock = threading.RLock()

    @staticmethod
    def _target(service: str, account: str) -> str:
        if not service or not account:
            raise ValueError('Credential service and account are required')
        return f'{service}:{account}'

    @staticmethod
    def _backend():
        try:
            import win32cred
        except ImportError as exc:
            raise RuntimeError('PunchCard credential storage requires Windows and pywin32') from exc
        return win32cred

    def get_password(self, service: str, account: str) -> str | None:
        cred = self._backend()
        target = self._target(service, account)
        with self._lock:
            try:
                stored = cred.CredRead(target, cred.CRED_TYPE_GENERIC)
            except Exception as exc:
                # Windows returns ERROR_NOT_FOUND (1168) when no credential exists.
                # Authentication or storage failures must not be mistaken for missing tokens.
                if getattr(exc, 'winerror', None) == 1168:
                    return None
                raise
        if stored is None:
            return None
        blob = stored['CredentialBlob']
        # pywin32 returns raw UTF-16LE bytes on Windows; some backends return str.
        return blob.decode('utf-16-le') if isinstance(blob, bytes) else blob

    def set_password(self, service: str, account: str, token: str) -> None:
        if not token:
            raise ValueError('Cannot store an empty token')
        cred = self._backend()
        target = self._target(service, account)
        with self._lock:
            cred.CredWrite({
                'Type': cred.CRED_TYPE_GENERIC,
                'TargetName': target,
                'UserName': account,
                'CredentialBlob': token,
                'Persist': cred.CRED_PERSIST_LOCAL_MACHINE,
                'Comment': 'PunchCard API token',
            }, 0)

    def delete_password(self, service: str, account: str) -> None:
        cred = self._backend()
        target = self._target(service, account)
        with self._lock:
            try:
                cred.CredDelete(target, cred.CRED_TYPE_GENERIC)
            except Exception as exc:
                if getattr(exc, 'winerror', None) != 1168:
                    raise
