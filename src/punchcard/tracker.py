"""Own the currently active session and its transitions, never historical editing.

Every tracking operation persists before it emits a notification. Tracker has
no awareness of UI widgets or Tempo's HTTP implementation.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from .database import Database
from .events import EventHook


class SessionTracker:
    def __init__(self, database: Database):
        self.database = database
        self.lock = threading.RLock()
        self.on_change = EventHook('tracker.changed')
        self.on_session_completed = EventHook('tracker.session_completed')
        self._windows_locked = False
        self._call_present = False
        self._ignore_call_until_end = False
        self._resume_ticket: tuple[str, str] | None = None
        self.database.recover_interrupted_session()
        self.active_session: dict | None = self.database.current_session()

    def _finish_active(self, reason: str) -> tuple[str | None, dict | None]:
        """Persist a completed session; caller announces it after releasing lock."""
        previous = self.active_session
        boundary = self.database.close_session(reason)
        return boundary, ({**previous, 'ended_at': boundary, 'stop_reason': reason}
                          if previous is not None and boundary is not None else None)

    def _refresh_active(self) -> None:
        self.active_session = self.database.current_session()

    def _notify(self, completed: dict | None = None) -> None:
        if completed is not None:
            self.on_session_completed.emit(completed.copy())
        self.on_change.emit()

    def start_ticket_session(self, key: str, title: str) -> None:
        with self.lock:
            current = self.active_session
            if current and current['kind'] == 'ticket' and current['ticket_key'] == key:
                return
            if self._call_present and current and current['kind'] == 'meeting':
                self._resume_ticket = (key, title)
                self.database.set_preference('last_ticket', key)
                completed = None
            else:
                boundary, completed = self._finish_active('ticket_switch')
                self.database.start_session('ticket', key, title, when=boundary)
                self.database.set_preference('last_ticket', key)
                self._refresh_active()
        self._notify(completed)

    def start_manual_meeting(self, title: str = 'Teams call') -> None:
        with self.lock:
            boundary, completed = self._finish_active('meeting_started')
            self.database.start_session('meeting', None, title, when=boundary)
            self._refresh_active()
        self._notify(completed)

    def on_teams_call_started(self, title: str, metadata: dict) -> None:
        with self.lock:
            self._call_present = True
            if self._windows_locked or self._ignore_call_until_end:
                return
            current = self.active_session
            if current and current['kind'] == 'meeting':
                self.database.update_active_meeting(title, metadata)
                self._refresh_active()
                completed = None
            else:
                self._resume_ticket = ((current['ticket_key'], current['title'])
                                       if current and current['kind'] == 'ticket' else None)
                boundary, completed = self._finish_active('teams_call_started')
                self.database.start_session('meeting', None, title, metadata=metadata, when=boundary)
                self._refresh_active()
        self._notify(completed)

    def on_teams_call_updated(self, title: str, metadata: dict) -> None:
        with self.lock:
            current = self.active_session
            if not (self._call_present and current and current['kind'] == 'meeting'):
                return
            self.database.update_active_meeting(title, metadata)
            self._refresh_active()
        self._notify()

    def on_teams_call_ended(self) -> None:
        with self.lock:
            self._call_present = False
            self._ignore_call_until_end = False
            current = self.active_session
            completed = None
            if current and current['kind'] == 'meeting':
                boundary, completed = self._finish_active('teams_call_ended')
                if self._resume_ticket and not self._windows_locked:
                    key, title = self._resume_ticket
                    self.database.start_session('ticket', key, title, when=boundary)
            self._resume_ticket = None
            self._refresh_active()
        self._notify(completed)

    def on_windows_locked(self) -> None:
        with self.lock:
            self._windows_locked = True
            self._resume_ticket = None
            self._ignore_call_until_end = self._call_present
            _, completed = self._finish_active('windows_lock')
            self._refresh_active()
        self._notify(completed)

    def on_windows_unlocked(self) -> None:
        with self.lock:
            self._windows_locked = False
            # Never resume a ticket automatically on unlock.
        self._notify()

    def pause(self, reason: str = 'manual') -> None:
        with self.lock:
            current = self.active_session
            if self._call_present and current and current['kind'] == 'meeting':
                self._ignore_call_until_end = True
                self._resume_ticket = None
            _, completed = self._finish_active(reason)
            self._refresh_active()
        self._notify(completed)

    def update_active_note(self, session_id: int, note: str) -> None:
        """Edit the exact active session, not whichever session starts next."""
        with self.lock:
            current = self.active_session
            if not current or current['id'] != session_id:
                raise ValueError('The tracking session has changed. Edit its notes in Timesheet instead.')
            if current.get('note', '') == note:
                return
            self.database.update_active_session_note(session_id, note)
            self._refresh_active()
        self._notify()

    def checkpoint(self) -> None:
        with self.lock:
            self.database.checkpoint_active_session()

    def status(self) -> dict:
        with self.lock:
            current = self.active_session.copy() if self.active_session else None
            elapsed = 0.0
            if current:
                started = datetime.fromisoformat(current['started_at'])
                elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
            return {'current': current, 'elapsed_seconds': elapsed,
                    'last_ticket': self.database.get_preference('last_ticket')}
