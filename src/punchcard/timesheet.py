"""Historical sessions: editing, cached records, summaries and CSV export.

There is no NiceGUI dependency here. Each TimesheetView owns its displayed day;
Timesheet owns the cached historical records and keeps them consistent with SQLite.
"""
from __future__ import annotations

import csv
import io
import threading
from datetime import date, datetime, time, timedelta, timezone

from .database import Database
from .events import EventHook
from .timestamps import local_datetime


class Timesheet:
    def __init__(self, database: Database):
        self.database = database
        self._lock = threading.RLock()
        self._days: dict[date, list[dict]] = {}
        self.on_change = EventHook('timesheet.changed')

    def load_day(self, day: date) -> list[dict]:
        """Refresh one day from the source of truth and cache the result."""
        rows = self.database.list_sessions(day)
        with self._lock:
            self._days[day] = rows
        return [row.copy() for row in rows]

    @staticmethod
    def session_seconds_in_period(row: dict, start: date, end: date,
                                  *, now: datetime | None = None) -> float:
        """Actual elapsed seconds within inclusive local dates, not wall time.

        UTC subtraction is essential for daylight-saving transitions. An active
        session is clipped at the current instant without changing its record.
        """
        if end < start:
            raise ValueError('End date must be on or after start date')
        first = datetime.combine(start, time.min).astimezone()
        after_last = datetime.combine(end + timedelta(days=1), time.min).astimezone()
        # Compare instants in UTC: Python wall-time subtraction can be wrong
        # when two datetimes share a zone across a DST clock change.
        begun = datetime.fromisoformat(row['started_at']).astimezone(timezone.utc)
        finished = (datetime.fromisoformat(row['ended_at']).astimezone(timezone.utc)
                    if row['ended_at'] else (now or datetime.now(timezone.utc)).astimezone(timezone.utc))
        lo = max(begun, first.astimezone(timezone.utc))
        hi = min(finished, after_last.astimezone(timezone.utc))
        return max(0.0, (hi - lo).total_seconds())

    def period_signature(self, start: date, end: date) -> tuple:
        return self.database.timesheet_signature_range(start, end)

    def sessions_in_range(self, start: date, end: date) -> list[dict]:
        return self.database.list_sessions_range(start, end)

    def cached_day(self, day: date) -> list[dict] | None:
        """Read a cache snapshot, or None when the day hasn't been loaded."""
        with self._lock:
            rows = self._days.get(day)
            return [row.copy() for row in rows] if rows is not None else None

    def invalidate(self) -> None:
        """Tracking or external sync has changed historical-session presentation."""
        with self._lock:
            self._days.clear()
        self.on_change.emit()

    def _refresh_loaded_days(self) -> None:
        """After an edit, refresh all previously loaded dates (edits can move days)."""
        with self._lock:
            days = tuple(self._days)
        replacements = {day: self.database.list_sessions(day) for day in days}
        with self._lock:
            self._days.update(replacements)
        self.on_change.emit()

    def edit_session(self, session_id: int, **changes) -> None:
        """Persist, then update our own cached state and announce the change."""
        self.database.edit_session(session_id, **changes)
        self._refresh_loaded_days()

    def delete_session(self, session_id: int) -> None:
        self.database.delete_session(session_id)
        self._refresh_loaded_days()

    def has_sessions(self) -> bool:
        return bool(self.database.list_sessions())

    def day_signature(self, day: date) -> tuple:
        """Compact revision for changes made by the tracker or sync worker."""
        return self.database.timesheet_signature(day)

    def sync_detail(self, session_id: int) -> dict | None:
        return self.database.sync_detail(session_id)

    def sync_cutoff(self) -> str:
        return self.database.get_preference('tempo_sync_after')

    def has_legacy_tempo_rollups(self) -> bool:
        return self.database.has_daily_rollups()

    def total_seconds(self, day: date) -> float:
        rows = self.cached_day(day)
        if rows is None:
            rows = self.load_day(day)
        return sum(self.session_seconds_in_period(row, day, day) for row in rows)

    def export_csv(self, start: date | None = None, end: date | None = None) -> bytes:
        """Export each session overlapping the requested inclusive date range once.

        Without dates, export the full history for backwards compatibility.
        Session timestamps/durations remain the complete recorded session,
        including any portion outside the selected range.
        """
        if (start is None) != (end is None):
            raise ValueError('Provide both start and end dates')
        if start is not None and end < start:
            raise ValueError('End date must be on or after start date')
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['session_id', 'date', 'start_local', 'end_local',
                         'duration_minutes', 'activity_type', 'issue_key', 'title', 'note',
                         'session_state', 'metadata_json'])
        snapshot_time = datetime.now(timezone.utc)
        rows = (self.database.list_sessions() if start is None
                else self.database.list_sessions_range(start, end))
        for row in reversed(rows):
            start = local_datetime(row['started_at'])
            is_active = row['ended_at'] is None
            end = snapshot_time.astimezone() if is_active else local_datetime(row['ended_at'])
            minutes = round(max(0, (end - start).total_seconds()) / 60, 2)
            writer.writerow([
                row['id'], start.date().isoformat(),
                start.isoformat(timespec='seconds'), end.isoformat(timespec='seconds'),
                minutes, row['kind'], row['ticket_key'] or '', row['title'], row['note'],
                'active_snapshot' if is_active else 'completed', row.get('metadata_json', '{}'),
            ])
        return ('\ufeff' + out.getvalue()).encode('utf-8')
