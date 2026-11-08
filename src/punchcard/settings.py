"""Authoritative runtime settings, API connections and credential state.

Configuration writes are atomic. Jira and Tempo tokens are stored in Windows
Credential Manager; cached settings are reloaded before use so background
workers observe changes saved from the UI.
"""
import json
import math
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from .credentials import CredentialVault
from .events import EventHook
from .timestamps import utcnow



KEYRING_SERVICE = 'PunchCard Jira'
TEMPO_SERVICE = 'PunchCard Tempo'


def _default_project_directory() -> Path:
    """Use the same per-user data directory for uv and frozen builds."""
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        return Path(local_app_data) / 'PunchCard'
    if os.name == 'nt' or getattr(sys, 'frozen', False):
        return Path.home() / 'AppData' / 'Local' / 'PunchCard'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share')) / 'PunchCard'


@dataclass(frozen=True)
class ProjectPaths:
    """Locate per-user data and separately locate bundled/source resources."""
    project_dir: Path = field(default_factory=_default_project_directory)

    @property
    def data_dir(self) -> Path:
        return self.project_dir / 'data'

    @property
    def database(self) -> Path:
        return self.data_dir / 'punchcard.db'

    @property
    def settings(self) -> Path:
        return self.data_dir / 'settings.json'

    @property
    def user_guide(self) -> Path:
        if getattr(sys, 'frozen', False):
            return Path(sys._MEIPASS) / 'USER_GUIDE.md'
        # The guide is a source resource, not writable application data.
        return Path(__file__).resolve().parents[2] / 'USER_GUIDE.md'


class Settings:
    """One object owns the current settings and the operations that modify them."""

    def __init__(self, database, *, config_path=None, vault=None, paths=None):
        self.database = database
        self.paths = paths or ProjectPaths()
        self.config_path = Path(config_path) if config_path is not None else self.paths.settings
        self.vault = vault if vault is not None else CredentialVault()
        self.on_change = EventHook("settings.changed")
        self._lock = threading.RLock()
        self.data = self.load()

    def load(self) -> dict:
        """Reload configuration to observe changes made by the active UI."""
        with self._lock:
            if self.config_path.exists():
                self.data = json.loads(self.config_path.read_text(encoding='utf-8'))
            else:
                self.data = {'jira_url': '', 'jira_email': ''}
            return self.data.copy()

    def save(self, values: dict) -> None:
        """Persist settings atomically; never expose a partial JSON document."""
        with self._lock:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            pending = self.config_path.with_suffix('.json.tmp')
            pending.write_text(json.dumps(values, indent=2), encoding='utf-8')
            pending.replace(self.config_path)
            self.data = values.copy()
        self.on_change.emit(self.data.copy())

    def token_expiry_value(self, kind: str) -> str:
        return token_expiry_value(kind, settings=self.load())

    def expiry_status(self, kind: str) -> dict:
        return expiry_status(kind, settings=self.load())

    def expiring_tokens(self) -> list[str]:
        return expiring_tokens(settings=self.load())

    def save_jira_connection(self, jira_url: str, jira_email: str, token: str = '',
                      token_expiry_days: int | str | None = None,
                      token_expiry_date: str | date | None = None) -> None:
        url = jira_url.strip().rstrip('/')
        email = jira_email.strip()
        if not url.startswith('https://'):
            raise ValueError('Jira URL must begin with https://')
        if not email:
            raise ValueError('Jira email is required')
        token = token.strip()
        days = _expiry_days(token_expiry_days)
        chosen_date = _parse_expiry_date(token_expiry_date)
        if days is not None and chosen_date is not None:
            raise ValueError('Provide a token expiry date, not both a date and a duration')
        if token and days is None and chosen_date is None:
            raise ValueError('Choose a Jira token expiry date before saving the new token')
        if token and chosen_date is not None and date.fromisoformat(chosen_date) < date.today():
            raise ValueError('A new Jira token cannot have a past expiry date')
        if (days is not None or chosen_date is not None) and not token and not self.vault.get_password(KEYRING_SERVICE, f'{url}|{email}'):
            raise ValueError('Save a Jira API token before setting its expiry')
        if token:
            self.vault.set_password(KEYRING_SERVICE, f'{url}|{email}', token)
        current = self.load()
        if (current.get('jira_url', '').rstrip('/'), current.get('jira_email', '')) != (url, email):
            # Expiry metadata belongs to the old Jira identity, not the new one.
            current.pop('jira_token_expiry_date', None)
            current.pop('jira_token_expires_at', None)
            current.pop('jira_token_validity_days_at_save', None)
        current.update({'jira_url': url, 'jira_email': email})
        if days is not None:
            _set_expiry(current, 'jira', days)
        elif chosen_date is not None:
            _store_expiry_date(current, 'jira', chosen_date)
        self.save(current)


    def get_jira_credentials(self) -> tuple[str, str, str]:
        settings = self.load()
        url = settings.get('jira_url', '').rstrip('/')
        email = settings.get('jira_email', '')
        if not url or not email:
            raise ValueError('Configure Jira URL and email in Settings first')
        token = self.vault.get_password(KEYRING_SERVICE, f'{url}|{email}')
        if not token:
            raise ValueError('No Jira API token saved; enter one in Settings')
        return url, email, token


    def _tempo_account_id_available(self, current: dict) -> bool:
        return bool(self.database.get_preference('jira_account_id') or current.get('tempo_account_id'))

    def save_tempo_connection(self, *, token: str = '', account_id: str | None = None,
                              enabled: bool | None = None,
                              api_url: str = 'https://api.tempo.io',
                              token_expiry_days: int | str | None = None,
                              token_expiry_date: str | date | None = None) -> None:
        """Save the connection; automatically enable sync unless opted out.

        A newly configured Tempo connection is sync-ready by default once Jira
        has supplied the Atlassian account ID. The Timesheet switch remains an
        explicit opt-out, which later token saves must respect.
        """
        url = api_url.strip().rstrip('/')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or not (
            parsed.hostname == 'api.tempo.io' or parsed.hostname.endswith('.tempo.io')
        ) or parsed.username or parsed.password:
            raise ValueError('Use an HTTPS Tempo API URL (api.tempo.io or a Tempo regional endpoint)')
        token = token.strip()
        days = _expiry_days(token_expiry_days)
        chosen_date = _parse_expiry_date(token_expiry_date)
        if days is not None and chosen_date is not None:
            raise ValueError('Provide a token expiry date, not both a date and a duration')
        if token and days is None and chosen_date is None:
            raise ValueError('Choose a Tempo token expiry date before saving the new token')
        if token and chosen_date is not None and date.fromisoformat(chosen_date) < date.today():
            raise ValueError('A new Tempo token cannot have a past expiry date')

        saved_token = self.vault.get_password(TEMPO_SERVICE, 'user_token')
        if not (token or saved_token):
            raise ValueError('Enter your Tempo API token before saving')

        current = self.load()
        if account_id is not None:
            current['tempo_account_id'] = account_id.strip()
        has_account = self._tempo_account_id_available(current)
        if enabled is True and not has_account:
            raise ValueError('Save Jira settings and refresh tickets to detect your account ID')

        was_enabled = bool(current.get('tempo_sync_enabled', False))
        # Explicitly disabling sync in Timesheet is authoritative. Merely
        # updating a token or its expiry must never override that choice.
        wants_sync = (not current.get('tempo_sync_manually_disabled', False)
                      if enabled is None else bool(enabled))
        enable_sync = bool(wants_sync and has_account)
        if token:
            self.vault.set_password(TEMPO_SERVICE, 'user_token', token)
        current.update({'tempo_api_url': url, 'tempo_sync_enabled': enable_sync})
        if days is not None:
            _set_expiry(current, 'tempo', days)
        elif chosen_date is not None:
            _store_expiry_date(current, 'tempo', chosen_date)

        if enable_sync and not was_enabled:
            # Persist the cutoff BEFORE enabling the worker; never backfill the
            # user's historic worklogs just because a connection was saved.
            self.database.set_preference('tempo_sync_after', utcnow())
        self.save(current)
        if enable_sync and not was_enabled:
            self.database.notify_change()

    def enable_tempo_sync_if_ready(self, *_args) -> None:
        """Activate a saved connection after Jira discovers the account ID.

        Called from Jira's refresh notification; an explicit opt-out wins.
        """
        current = self.load()
        if (current.get('tempo_sync_enabled', False)
                or current.get('tempo_sync_manually_disabled', False)
                or not self._tempo_account_id_available(current)
                or not self.vault.get_password(TEMPO_SERVICE, 'user_token')):
            return
        self.database.set_preference('tempo_sync_after', utcnow())
        current['tempo_sync_enabled'] = True
        self.save(current)
        self.database.notify_change()

    def set_tempo_sync_enabled(self, enabled: bool) -> None:
        """Explicitly control sync from the Timesheet, independent of form saves."""
        current = self.load()
        enabled = bool(enabled)
        was_enabled = bool(current.get('tempo_sync_enabled', False))
        opted_out = not enabled
        if enabled:
            if not self.vault.get_password(TEMPO_SERVICE, 'user_token'):
                raise ValueError('Save your Tempo API token before enabling sync')
            if not self._tempo_account_id_available(current):
                raise ValueError('Save Jira settings and refresh tickets to detect your account ID')
        if enabled == was_enabled and current.get('tempo_sync_manually_disabled', False) == opted_out:
            return
        if enabled and not was_enabled:
            self.database.set_preference('tempo_sync_after', utcnow())
        current['tempo_sync_enabled'] = enabled
        current['tempo_sync_manually_disabled'] = opted_out
        self.save(current)
        self.database.notify_change()

    def get_tempo_api_credentials(self) -> tuple[str, str]:
        """Connection details for any Tempo API feature, independent of sync opt-in."""
        current = self.load()
        token = self.vault.get_password(TEMPO_SERVICE, 'user_token')
        if not token:
            raise ValueError('Configure a Tempo API token in Settings first')
        return current.get('tempo_api_url', 'https://api.tempo.io').rstrip('/'), token

    def get_tempo_credentials(self) -> tuple[str, str, str]:
        """Sync also requires an Atlassian account to attribute worklogs to."""
        base_url, token = self.get_tempo_api_credentials()
        account_id = self.database.get_preference('jira_account_id') or self.load().get('tempo_account_id')
        if not account_id:
            raise ValueError('Tempo needs your Jira account ID; refresh your Jira tickets first')
        return base_url, token, account_id


# Private token-expiry calculations; Settings owns the public operations.

def _expiry_days(value: int | str | None) -> int | None:
    """Parse the *remaining* validity at the moment the user saves a token."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError('Token expiry must be a positive number of whole days')
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError('Token expiry must be a positive number of whole days') from exc
    if days < 1 or str(days) != str(value).strip():
        raise ValueError('Token expiry must be a positive number of whole days')
    return days


def _set_expiry(settings: dict, prefix: str, days: int, *, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    settings[f'{prefix}_token_expires_at'] = (now + timedelta(days=days)).isoformat()
    settings[f'{prefix}_token_validity_days_at_save'] = days


def _parse_expiry_date(value: str | date | None) -> str | None:
    """Validate an actual calendar expiry date (not a duration from today)."""
    if value is None or value == '':
        return None
    try:
        parsed = value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('Choose a valid token expiry date') from exc
    return parsed.isoformat()


def _store_expiry_date(settings: dict, kind: str, value: str) -> None:
    settings[f'{kind}_token_expiry_date'] = value
    # The user now supplies the actual calendar date. Do not carry an older
    # days-based estimate alongside it.
    settings.pop(f'{kind}_token_expires_at', None)
    settings.pop(f'{kind}_token_validity_days_at_save', None)


def token_expiry_value(kind: str, *, settings: dict | None = None) -> str:
    """The YYYY-MM-DD value to display in a native date picker."""
    if kind not in ('jira', 'tempo'):
        raise ValueError('Unknown credential type')
    settings = {} if settings is None else settings
    value = settings.get(f'{kind}_token_expiry_date', '')
    if value:
        try:
            return date.fromisoformat(value).isoformat()
        except (TypeError, ValueError):
            return ''
    # Display older test configurations faithfully until the user chooses a date.
    old = settings.get(f'{kind}_token_expires_at')
    try:
        return datetime.fromisoformat(old.replace('Z', '+00:00')).astimezone().date().isoformat() if old else ''
    except (TypeError, ValueError, OverflowError):
        return ''


def expiry_status(kind: str, *, settings: dict | None = None,
                  now: datetime | None = None) -> dict:
    """Return best-effort expiry status; no tokens are decrypted.

    Dates are supplied by the user; actual server-side token revocation
    may occur earlier. Legacy days-based metadata is also understood.
    """
    if kind not in ('jira', 'tempo'):
        raise ValueError('Unknown credential type')
    settings = {} if settings is None else settings
    calendar_date = settings.get(f'{kind}_token_expiry_date')
    if calendar_date is not None:
        try:
            selected = date.fromisoformat(calendar_date)
            reference = (now or datetime.now(timezone.utc)).astimezone().date()
            remaining = (selected - reference).days
        except (ValueError, TypeError, OverflowError):
            return {'state': 'unknown', 'days_remaining': None,
                    'text': 'Expiry date invalid', 'expires_at': None}
        label = selected.strftime('%d %b %Y')
        if remaining < 0:
            return {'state': 'expired', 'days_remaining': 0,
                    'text': f'Expired {abs(remaining)} day(s) ago ({label})', 'expires_at': selected}
        if remaining == 0:
            return {'state': 'warning', 'days_remaining': 0,
                    'text': f'Expires today ({label})', 'expires_at': selected}
        state = 'warning' if remaining <= 7 else 'valid'
        return {'state': state, 'days_remaining': remaining,
                'text': f'{remaining} days remaining ({label})', 'expires_at': selected}
    expiry = settings.get(f'{kind}_token_expires_at')
    if not expiry:
        return {'state': 'unknown', 'days_remaining': None,
                'text': 'Expiry not recorded', 'expires_at': None}
    try:
        dt = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('Timezone missing')
        remaining = (dt - (now or datetime.now(timezone.utc))).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return {'state': 'unknown', 'days_remaining': None,
                'text': 'Expiry date invalid', 'expires_at': None}
    display_date = dt.astimezone().strftime('%d %b %Y')
    if remaining <= 0:
        days_ago = math.ceil(abs(remaining) / 86400)
        return {'state': 'expired', 'days_remaining': 0,
                'text': f'Estimated expiry passed {days_ago} day(s) ago ({display_date})',
                'expires_at': dt}
    days_left = math.ceil(remaining / 86400)
    state = 'warning' if remaining <= 7 * 86400 else 'valid'
    text = (f'Less than 24 hours remaining ({display_date})' if remaining < 86400
            else f'About {days_left} days remaining ({display_date})')
    return {'state': state, 'days_remaining': days_left,
            'text': text, 'expires_at': dt}


def expiring_tokens(*, settings: dict | None = None, now: datetime | None = None) -> list[str]:
    """Only surface urgent expiry notices in the compact widget."""
    settings = {} if settings is None else settings
    warnings = []
    for kind, label in (('jira', 'Jira'), ('tempo', 'Tempo')):
        info = expiry_status(kind, settings=settings, now=now)
        if info['state'] in ('warning', 'expired'):
            warnings.append(f'{label}: {info["text"]}')
    return warnings
