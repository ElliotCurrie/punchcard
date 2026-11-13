"""Tempo Cloud V4 API reconciliation: create, update, delete and verify worklogs."""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests
from .database import Database
from .settings import TEMPO_SERVICE
from .events import EventHook
from .connection_errors import network_error
from .timestamps import local_datetime

LOG = logging.getLogger(__name__)

class TempoSyncDisabled(Exception):
    """Raised when automatic synchronisation is switched off."""

class Tempo:
    def __init__(self, settings, database: Database, *, http: requests.Session | None = None,
                 retry_interval: float = 90, verification_interval: float = 15 * 60):
        self.settings = settings
        self.on_change = EventHook("tempo.sync_changed")
        self.database = database
        self.http = http
        # One Tempo instance owns request pacing and any future API caches.
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0
        self.retry_interval = retry_interval
        self.verification_interval = verification_interval
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_verify = time.monotonic()

    def describe_session_sync(self, session: dict, sync: dict | None, *,
                              enabled: bool, cutoff: str) -> dict:
        """Explain a session's Tempo status to any presentation, not just Timesheet."""
        return session_sync_display(session, sync, enabled=enabled, cutoff=cutoff)

    def format_session_minutes(self, session: dict) -> str:
        """Display the same minute boundaries used for Tempo worklogs."""
        return timesheet_minute_range(session)

    def _enabled(self) -> bool:
        return bool(self.settings.load().get('tempo_sync_enabled', False))

    def _request(self, client, method: str, url: str, *, require_sync: bool = True, **kwargs):
        """Send a paced Tempo request; sync writes must respect the opt-in switch."""
        if require_sync and not self._enabled():
            raise TempoSyncDisabled()
        if self.http is None:
            with self._request_lock:
                remaining = self._next_request_at - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                self._next_request_at = time.monotonic() + 0.26
                # Switching off while pacing must also prevent the next sync write.
                if require_sync and not self._enabled():
                    raise TempoSyncDisabled()
        return client.request(method, url, **kwargs)

    def request_api(self, method: str, endpoint: str, **kwargs):
        """Authenticated Tempo API transport for new and existing features.

        API reads aren't disabled just because automatic session sync is disabled.
        A new feature can call this and retain its response in this Tempo instance.
        """
        base_url, token = self.settings.get_tempo_api_credentials()
        client = self.http or requests.Session()
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        headers.update(kwargs.pop('headers', {}) or {})
        try:
            response = self._request(
                client, method, f'{base_url}/4/{endpoint.lstrip("/")}',
                require_sync=False, headers=headers, timeout=(5, 20), **kwargs,
            )
            response.raise_for_status()
            return response.json() if response.content else None
        finally:
            if self.http is None:
                client.close()

    def validate_connection(self, token: str, api_url: str) -> None:
        """Read-only Tempo check; never create a worklog during validation."""
        client = self.http or requests.Session()
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        try:
            response = self._request(
                client, 'GET', f'{api_url.rstrip("/")}/4/worklogs',
                require_sync=False, params={'limit': 1}, headers=headers,
                timeout=(5, 20),
            )
        except requests.RequestException as exc:
            raise ConnectionError(network_error('Tempo', api_url, exc)) from exc
        finally:
            if self.http is None:
                client.close()
        if response.status_code == 401:
            raise ValueError('Tempo rejected the API token (HTTP 401)')
        if response.status_code in (403, 404):
            raise PermissionError(
                f'Tempo denied access (HTTP {response.status_code}). '
                'Check Worklogs permission and the regional API endpoint.'
            )
        if not response.ok:
            raise ConnectionError(f'Tempo API unavailable (HTTP {response.status_code})')

    def save_connection(self, *, token: str = '', api_url: str = 'https://api.tempo.io',
                        token_expiry_date=None) -> None:
        """Validate then persist a Tempo connection; reject invalid replacements."""
        candidate = token.strip()
        existing = self.settings.load()
        old_url = existing.get('tempo_api_url', 'https://api.tempo.io').rstrip('/')
        old_token = self.settings.vault.get_password(TEMPO_SERVICE, 'user_token')
        candidate_token = candidate or old_token
        if not candidate_token:
            raise ValueError('Enter a Tempo API token first')
        url = api_url.strip().rstrip('/')
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or not parsed.hostname or
            not (parsed.hostname == 'api.tempo.io' or
                 parsed.hostname.endswith('.tempo.io')) or
            parsed.username or parsed.password):
            raise ValueError('Use an HTTPS Tempo API URL (api.tempo.io or a Tempo regional endpoint)')
        try:
            self.validate_connection(candidate_token, url)
        except (ValueError, PermissionError, ConnectionError) as exc:
            # Do not mark the saved connection lost when a *replacement* fails.
            if not candidate and old_url == url:
                self.settings.record_connection_check('tempo', success=False, detail=str(exc))
            raise
        self.settings.save_tempo_connection(
            token=candidate, api_url=url, token_expiry_date=token_expiry_date,
            validated=True,
        )

    def check_connection(self) -> None:
        """Recheck the saved credentials, retaining the user's manual sync choice."""
        try:
            url, token = self.settings.get_tempo_api_credentials()
            self.validate_connection(token, url)
        except (ValueError, PermissionError, ConnectionError) as exc:
            self.settings.record_connection_check('tempo', success=False, detail=str(exc))
            raise
        self.settings.record_connection_check('tempo', success=True,
            detail='Tempo API verified successfully.')
        self.settings.enable_tempo_sync_if_ready()

    def _verify_day(self, client, base_url: str, headers: dict, day: str, cutoff: str,
                    counts: dict, max_verifications: int = 50):
        """Periodic reconciliation of remote IDs, including unchanged sessions."""
        checked = 0
        for row in reversed(self.database.sync_rows(after=cutoff)):
            if not self._enabled():
                return
            if checked >= max_verifications:
                break
            remote_id = row.get('remote_id')
            if (not remote_id or row['sync_state'] in ('sending', 'uncertain') or
                    local_datetime(row['started_at']).date().isoformat() != day):
                continue
            checked += 1
            try:
                response = self._request(client, 'GET', f'{base_url}/4/worklogs/{remote_id}',
                                         headers=headers, timeout=(5, 20))
            except TempoSyncDisabled:
                return
            except requests.RequestException as exc:
                LOG.warning('Tempo verification failed for session %s: %s', row['id'], exc)
                counts['failed'] += 1
                continue
            if response.status_code == 404:
                if self.database.reset_missing_remote(row['id'], remote_id):
                    counts['missing'] += 1
            elif not response.ok:
                counts['failed'] += 1
                LOG.warning('Tempo verification HTTP %s for session %s', response.status_code, row['id'])

    def _delete(self, client, url: str, headers: dict, session_id: int, counts: dict, *, orphan=False, state='too_short', reason=None):
        """Delete only a PunchCard-linked worklog; never a foreign Tempo record."""
        if not self.database.claim_delete(session_id):
            return
        try:
            response = self._request(client, 'DELETE', url, headers=headers, timeout=(5, 20))
            if response.status_code in (200, 204, 404):
                self.database.complete_delete(session_id, orphan=orphan, state=state, reason=reason)
                counts['deleted'] += 1
            else:
                uncertain = response.status_code >= 500 or response.status_code == 429
                self.database.fail_sync(session_id, f'HTTP {response.status_code}: {response.text[:200]}', uncertain=uncertain)
                counts['failed'] += 1
        except TempoSyncDisabled:
            self.database.release_sync_claim(session_id, orphan=orphan)
        except requests.RequestException as exc:
            self.database.fail_sync(session_id, f'{type(exc).__name__}: {exc}', uncertain=True)
            counts['failed'] += 1

    def synchronise_pending_worklogs(self, max_records: int = 20, *, verify_day: str | None = None) -> dict:
        settings = self.settings.load()
        counts = {k: 0 for k in ('created', 'updated', 'deleted', 'missing', 'failed', 'skipped', 'too_short')}
        counts['enabled'] = bool(settings.get('tempo_sync_enabled', False))
        counts['blocked'] = False
        if not counts['enabled']:
            return counts
        # UAT-only: older builds created combined day/ticket worklogs. We cannot
        # silently POST per-session replacements while those remote entries exist.
        if self.database.has_daily_rollups():
            counts['blocked'] = True
            return counts

        base_url, token, account_id = self.settings.get_tempo_credentials()
        cutoff = self.database.get_preference('tempo_sync_after')
        if not cutoff:
            return counts
        client = self.http or requests.Session()
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        processed = 0
        try:
            if verify_day is not None:
                self._verify_day(client, base_url, headers, verify_day, cutoff, counts)

            # Deleted local sessions keep a remote ID as a tombstone until the
            # corresponding Tempo DELETE succeeds.
            for row in self.database.sync_orphans():
                if not self._enabled():
                    return counts
                if processed >= max_records:
                    break
                if row['state'] in ('sending', 'uncertain', 'error'):
                    counts['skipped'] += 1
                    continue
                self._delete(client, f'{base_url}/4/worklogs/{row["remote_id"]}', headers,
                             row['session_id'], counts, orphan=True)
                processed += 1

            for row in self.database.sync_rows(after=cutoff):
                if not self._enabled():
                    return counts
                if processed >= max_records:
                    break
                if row['sync_state'] in ('sending', 'uncertain', 'error'):
                    counts['skipped'] += 1
                    continue
                slot = session_slot(row)
                remote_id = row.get('remote_id')
                reason = None
                state = 'too_short'
                if not row.get('ticket_key'):
                    reason, state = 'Unassigned session; not submitted to Tempo', 'unassigned'
                elif not str(row.get('jira_issue_id') or '').isdigit():
                    reason, state = 'Jira numeric issue ID unavailable; refresh Jira issues', 'missing_issue'
                elif slot is None:
                    reason = 'The minute-aligned interval is under one minute; kept locally'

                if reason:
                    if remote_id:
                        self._delete(client, f'{base_url}/4/worklogs/{remote_id}', headers,
                                     row['id'], counts, state=state, reason=reason)
                        processed += 1
                    else:
                        self.database.mark_unsyncable(row['id'], state, reason)
                    if state == 'too_short':
                        counts['too_short'] += 1
                    else:
                        counts['skipped'] += 1
                    continue

                data = worklog_payload(row, account_id)
                digest = fingerprint(data)
                if row['remote_id'] and row['sent_hash'] == digest and row['sync_state'] in ('synced', 'pending'):
                    if row['sync_state'] == 'pending':
                        self.database.mark_unchanged_synced(row['id'], digest)
                    continue
                action = self.database.claim_sync(row['id'], digest)
                if action is None:
                    continue
                processed += 1
                method = 'PUT' if action == 'update' else 'POST'
                url = f'{base_url}/4/worklogs' + (f'/{remote_id}' if method == 'PUT' else '')
                try:
                    response = self._request(client, method, url, headers=headers,
                                             json=data, timeout=(5, 20))
                    if method == 'PUT' and response.status_code == 404:
                        try:
                            check = self._request(client, 'GET', url, headers=headers, timeout=(5, 20))
                        except requests.RequestException as exc:
                            self.database.fail_sync(row['id'], f'404 verification failed: {exc}', uncertain=True)
                            counts['failed'] += 1
                            continue
                        if check.status_code == 404:
                            if self.database.reset_missing_remote(row['id'], remote_id):
                                counts['missing'] += 1
                        else:
                            self.database.fail_sync(row['id'], f'PUT 404; verification HTTP {check.status_code}',
                                                 uncertain=not check.ok)
                            counts['failed'] += 1
                        continue
                    if not response.ok:
                        uncertain = response.status_code >= 500 or response.status_code == 429
                        self.database.fail_sync(row['id'],
                            f'HTTP {response.status_code}: {response.text[:200]}', uncertain=uncertain)
                        counts['failed'] += 1
                        continue
                    if method == 'POST':
                        result = response.json()
                        saved_id = str(result.get('tempoWorklogId') or result.get('id') or '')
                    else:
                        # PUT may return 204 No Content; we already have the ID.
                        saved_id = str(remote_id)
                    if not saved_id:
                        self.database.fail_sync(row['id'], 'Tempo did not return a worklog ID; check Tempo', uncertain=True)
                        counts['failed'] += 1
                        continue
                    self.database.complete_sync(row['id'], saved_id, digest)
                    counts['created' if action == 'create' else 'updated'] += 1
                except TempoSyncDisabled:
                    self.database.release_sync_claim(row['id'])
                    return counts
                except (requests.RequestException, ValueError) as exc:
                    self.database.fail_sync(row['id'], f'{type(exc).__name__}: {exc}', uncertain=True)
                    counts['failed'] += 1
                    LOG.warning('Tempo response uncertain for session %s', row['id'])
        finally:
            if self.http is None:
                client.close()
        if any(counts[key] for key in ('created', 'updated', 'deleted', 'missing', 'failed', 'too_short')):
            self.on_change.emit(counts.copy())
        return counts

    def request_synchronisation(self) -> None:
        """Wake the single Tempo worker, on NiceGUI's event loop."""
        self._wake.set()

    def start(self) -> None:
        """Start one worker for the entire application lifetime."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._background_sync(), name='PunchCardTempoSync')

    async def stop(self) -> None:
        """Stop scheduling synchronisation when PunchCard shuts down."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _background_sync(self) -> None:
        """Sync on committed changes and periodically retry/verify remote worklogs."""
        self.request_synchronisation()  # Retry pending records left by a prior exit.
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.retry_interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            now = time.monotonic()
            verify_day = None
            if now - self._last_verify >= self.verification_interval:
                verify_day = date.today().isoformat()
                self._last_verify = now
            try:
                result = await asyncio.to_thread(
                    self.synchronise_pending_worklogs, verify_day=verify_day
                )
                if result.get('failed'):
                    LOG.warning('Tempo sync: %s failed worklog(s)', result['failed'])
                if result.get('blocked'):
                    LOG.warning('Tempo sync paused: old UAT daily worklogs require cleanup')
                if result.get('missing') or sum(result.get(key, 0)
                        for key in ('created', 'updated', 'deleted')) >= 20:
                    self.request_synchronisation()
            except Exception:
                LOG.exception('Tempo sync failed; sessions remain saved locally')

    def retry_failed_syncs(self) -> None:
        """Release locally failed worklogs for retry after credentials change."""
        self.database.retry_failed_syncs()
        self.request_synchronisation()


# Pure worklog calculations stay alongside their owning Tempo integration.
MIN_WORKLOG_SECONDS = 60
MIN_ACTIVITY_SECONDS = 30  # Ignore a 1-29-second activity flicker.

def round_boundary(value: str) -> datetime:
    """Return the closest real minute boundary, with 30 seconds rounding up.

    Round the UTC instant before converting to local wall-clock time so daylight
    saving changes do not make two actual instants appear to have wrong ordering.
    """
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError('Session timestamp must have a timezone')
    instant = instant.astimezone(timezone.utc)
    minute = instant.replace(second=0, microsecond=0)
    return minute + timedelta(minutes=1 if instant.second >= 30 else 0)


def session_slot(row: dict) -> dict | None:
    """Projected exact session interval for Tempo, or None if no full minute fits."""
    if not row.get('ended_at'):
        return None
    exact_start = datetime.fromisoformat(row['started_at'])
    exact_end = datetime.fromisoformat(row['ended_at'])
    if (exact_end - exact_start).total_seconds() < MIN_ACTIVITY_SECONDS:
        return None
    start = round_boundary(row['started_at'])
    end = round_boundary(row['ended_at'])
    if end <= start:
        return None
    local_start = start.astimezone()
    return {
        'started_at': local_start,
        'ended_at': end.astimezone(),
        'seconds': int((end - start).total_seconds()),
    }


def timesheet_minute_range(row: dict) -> str:
    """Display the same minute boundaries Tempo uses, without local seconds.

    For a running session there is no confirmed end boundary yet.
    Even sessions too short to send have minute-level times for consistency.
    """
    start = round_boundary(row['started_at']).astimezone()
    if not row.get('ended_at'):
        return f'{start:%H:%M}–now'
    end = round_boundary(row['ended_at']).astimezone()
    return f'{start:%H:%M}–{end:%H:%M}'


def worklog_payload(row: dict, account_id: str) -> dict:
    """Represent one completed session as one Tempo minute-aligned worklog."""
    if not row.get('ended_at') or not row.get('ticket_key') or not str(row.get('jira_issue_id') or '').isdigit():
        raise ValueError('A completed session linked to a Jira issue ID is required')
    slot = session_slot(row)
    if slot is None:
        raise ValueError('Session has no representable whole-minute interval in Tempo')
    description = (row.get('note') or row.get('title') or 'PunchCard activity').strip()
    # Identifies the source session when a response is ambiguous; not a server-side idempotency key.
    description = f'{description}\n[PunchCard session {row["id"]}]'
    start = slot['started_at']
    return {
        'issueId': int(row['jira_issue_id']),
        'authorAccountId': account_id,
        'startDate': start.strftime('%Y-%m-%d'),
        'startTime': start.strftime('%H:%M:00'),
        'timeSpentSeconds': slot['seconds'],
        'description': description,
    }


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


def session_sync_display(row: dict, sync: dict | None, *, enabled: bool, cutoff: str) -> dict:
    """Compact status; details remain in tooltips instead of timesheet prose."""
    state = (sync or {}).get('state')
    remote_id = (sync or {}).get('remote_id')
    if not row.get('ended_at'):
        return {'text': 'Recording', 'color': 'grey', 'queued': False}
    if state == 'sending':
        return {'text': 'Syncing', 'color': 'blue', 'queued': False}
    if state == 'uncertain':
        return {'text': 'Check Tempo', 'color': 'red', 'queued': False,
                'detail': 'Tempo may have received this worklog; automatic retry is disabled to prevent duplicates.'}
    if state == 'error':
        return {'text': 'Failed', 'color': 'red', 'queued': False,
                'detail': (sync or {}).get('error') or 'Tempo rejected this worklog.'}
    if state == 'synced':
        last = (sync or {}).get('last_synced_at')
        return {'text': 'Synced', 'color': 'green', 'queued': False,
                'detail': f'Synced at {local_datetime(last):%H:%M:%S}' if last else 'Synced to Tempo'}
    if remote_id and (not row.get('ticket_key') or session_slot(row) is None):
        detail = 'Remove linked worklog from Tempo'
    elif not row.get('ticket_key'):
        return {'text': 'Local', 'color': 'grey', 'queued': False, 'detail': 'No Jira issue assigned'}
    elif not remote_id and (not cutoff or row['started_at'] < cutoff):
        return {'text': 'Local', 'color': 'grey', 'queued': False, 'detail': 'Recorded before Tempo sync was enabled'}
    elif session_slot(row) is None:
        return {'text': 'Too short', 'color': 'orange', 'queued': False,
                'detail': 'No full minute can be represented in Tempo'}
    else:
        detail = 'Update linked Tempo worklog' if remote_id else 'Create Tempo worklog'
    if not enabled:
        return {'text': 'Local', 'color': 'grey', 'queued': False, 'detail': 'Tempo automatic sync is off'}
    return {'text': 'Pending', 'color': 'blue', 'queued': True, 'detail': detail}


