"""SQLite source of truth; always stores timezone-aware UTC timestamps."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from .timestamps import UTC, utcnow, local_datetime
from pathlib import Path

class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._change_callback = None
        self.initialise()

    @contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            finally:
                db.close()

    def initialise(self):
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tickets (
                    issue_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '',
                    assigned INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    parent_key TEXT,
                    issue_type TEXT NOT NULL DEFAULT 'Issue',
                    issue_type_icon TEXT NOT NULL DEFAULT '',
                    status_category TEXT NOT NULL DEFAULT '',
                    visible INTEGER NOT NULL DEFAULT 1,
                    jira_issue_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('ticket', 'meeting', 'other')),
                    ticket_key TEXT,
                    title TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    last_seen_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    stop_reason TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS ix_sessions_started ON sessions(started_at);
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tempo_sync (
                    session_id INTEGER PRIMARY KEY REFERENCES sessions(id),
                    remote_id TEXT,
                    sent_hash TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',
                    last_attempt_at TEXT,
                    last_synced_at TEXT,
                    error TEXT
                );
            ''')
            # Upgrade SQLite databases made by earlier PunchCard versions.
            existing = {row['name'] for row in db.execute('PRAGMA table_info(tickets)')}
            for name, declaration in {
                'parent_key': 'TEXT',
                'issue_type': "TEXT NOT NULL DEFAULT 'Issue'",
                'issue_type_icon': "TEXT NOT NULL DEFAULT ''",
                'status_category': "TEXT NOT NULL DEFAULT ''",
                'visible': 'INTEGER NOT NULL DEFAULT 1',
                'jira_issue_id': "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in existing:
                    db.execute(f'ALTER TABLE tickets ADD COLUMN {name} {declaration}')
            session_columns = {row['name'] for row in db.execute('PRAGMA table_info(sessions)')}
            if 'metadata_json' not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
            sync_columns = {row['name'] for row in db.execute('PRAGMA table_info(tempo_sync)')}
            if 'last_synced_at' not in sync_columns:
                db.execute('ALTER TABLE tempo_sync ADD COLUMN last_synced_at TEXT')
            # A killed process may have submitted a worklog just before it died.
            # Treat an interrupted submission as uncertain, never blindly POST it again.
            db.execute("UPDATE tempo_sync SET state='uncertain', error='Application interrupted during a Tempo request' WHERE state='sending'")

    def set_change_callback(self, callback):
        """Register a post-commit notifier for sync-relevant activity changes."""
        self._change_callback = callback

    def notify_change(self):
        callback = self._change_callback
        if callback is not None:
            callback()

    def set_preference(self, key: str, value: str):
        with self.connection() as db:
            db.execute('INSERT INTO preferences(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def get_preference(self, key: str, default: str = '') -> str:
        with self.connection() as db:
            row = db.execute('SELECT value FROM preferences WHERE key=?', (key,)).fetchone()
            return row['value'] if row else default

    def save_tickets(self, tickets: list[dict]):
        now = utcnow()
        with self.connection() as db:
            db.execute('UPDATE tickets SET visible=0, assigned=0')
            db.executemany("""INSERT INTO tickets(
                issue_key,title,status,assigned,updated_at,parent_key,
                issue_type,issue_type_icon,status_category,visible,jira_issue_id)
                VALUES(?,?,?,?,?,?,?,?,?,1,?)
                ON CONFLICT(issue_key) DO UPDATE SET
                title=excluded.title,
                status=excluded.status,
                assigned=excluded.assigned,
                updated_at=excluded.updated_at,
                parent_key=excluded.parent_key,
                issue_type=excluded.issue_type,
                issue_type_icon=excluded.issue_type_icon,
                status_category=excluded.status_category,
                jira_issue_id=excluded.jira_issue_id,
                visible=1""",
                [(t['key'], t['title'], t.get('status', ''),
                  int(t.get('assigned', True)), now, t.get('parent_key'),
                  t.get('issue_type', 'Issue'), t.get('issue_type_icon', ''),
                  t.get('status_category', ''), str(t.get('jira_issue_id') or '')) for t in tickets])
            # Keep old issues cached for historic worklogs, but not in picker.
            db.execute('INSERT INTO preferences(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('last_ticket_refresh', now))
            account_id = next((t.get('account_id') for t in tickets if t.get('assigned') and t.get('account_id')), None)
            if account_id:
                db.execute("INSERT INTO preferences(key,value) VALUES('jira_account_id', ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (account_id,))

    def list_tickets(self) -> list[dict]:
        """Latest assigned issues and their parents, excluding stale cached issues."""
        with self.connection() as db:
            rows = db.execute("""SELECT issue_key,title,status,assigned,parent_key,
                issue_type,issue_type_icon,status_category,jira_issue_id
                FROM tickets WHERE visible=1 ORDER BY issue_key""").fetchall()
            return [dict(row) for row in rows]

    def current_session(self) -> dict | None:
        with self.connection() as db:
            row = db.execute('SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1').fetchone()
            return dict(row) if row else None

    def start_session(self, kind: str, ticket_key: str | None, title: str, metadata: dict | None = None, *, when: str | None = None):
        if kind not in ('ticket', 'meeting', 'other'):
            raise ValueError('Invalid activity type')
        with self.connection() as db:
            opened = db.execute('SELECT id FROM sessions WHERE ended_at IS NULL').fetchone()
            if opened:
                raise RuntimeError('Another session is already running')
            now = when or utcnow()
            db.execute('''INSERT INTO sessions(kind,ticket_key,title,started_at,last_seen_at,metadata_json)
                VALUES(?,?,?,?,?,?)''', (kind, ticket_key, title, now, now,
                                         json.dumps(metadata or {}, ensure_ascii=False)))

    def update_active_meeting(self, title: str, metadata: dict):
        # Updates metadata without affecting the clock.
        with self.connection() as db:
            db.execute('''UPDATE sessions SET title=?, metadata_json=?
                          WHERE ended_at IS NULL AND kind='meeting' ''',
                       (title, json.dumps(metadata, ensure_ascii=False)))

    def close_session(self, reason: str = 'manual', when: str | None = None):
        with self.connection() as db:
            row = db.execute('SELECT id, started_at FROM sessions WHERE ended_at IS NULL').fetchone()
            if row is None:
                return None
            end = when or utcnow()
            if datetime.fromisoformat(end) < datetime.fromisoformat(row['started_at']):
                end = row['started_at']
            db.execute('UPDATE sessions SET ended_at=?,last_seen_at=?,stop_reason=? WHERE id=?',
                       (end, end, reason, row['id']))
        self.notify_change()
        return end

    def checkpoint_active_session(self):
        with self.connection() as db:
            db.execute('UPDATE sessions SET last_seen_at=? WHERE ended_at IS NULL', (utcnow(),))

    def recover_interrupted_session(self):
        """Do not count overnight/crash downtime as active work."""
        with self.connection() as db:
            db.execute('''UPDATE sessions SET ended_at=last_seen_at, stop_reason='unexpected_exit'
                          WHERE ended_at IS NULL''')

    def list_sessions(self, day: date | None = None) -> list[dict]:
        """Return session records, newest first; optionally restrict to a local day."""
        if day is not None:
            return self.list_sessions_range(day, day)
        with self.connection() as db:
            rows = db.execute('SELECT * FROM sessions ORDER BY started_at DESC').fetchall()
        return [dict(row) for row in rows]

    def list_sessions_range(self, start: date, end: date) -> list[dict]:
        """Unique sessions overlapping inclusive local calendar dates.

        A session crossing midnight is returned once for the whole range, but is
        visible on either individual day. Existing data uses UTC timestamps.
        """
        if end < start:
            raise ValueError('End date must be on or after start date')
        first = datetime.combine(start, time.min).astimezone()
        after_last = datetime.combine(end + timedelta(days=1), time.min).astimezone()
        snapshot = datetime.now().astimezone()
        return [row for row in self.list_sessions()
                if local_datetime(row['started_at']) < after_last
                and (local_datetime(row['ended_at']) if row['ended_at'] else snapshot) > first]

    def edit_session(self, session_id: int, *, started_at: str, ended_at: str, ticket_key: str | None, note: str, title: str | None = None):
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError('End must be after start, with timezone-aware timestamps')
        with self.connection() as db:
            row = db.execute('SELECT id, kind, title, started_at, ended_at, ticket_key, note FROM sessions WHERE id=?',
                             (session_id,)).fetchone()
            if row is None:
                raise ValueError('Session not found')
            if row['ended_at'] is None:
                raise ValueError('Pause the active session before editing')
            linked = db.execute('SELECT remote_id, state FROM tempo_sync WHERE session_id=?', (session_id,)).fetchone()
            if linked and linked['state'] in ('sending', 'uncertain'):
                raise ValueError('Resolve the in-flight/uncertain Tempo result before editing this session')
            start_utc = start.astimezone(UTC).isoformat(timespec='seconds')
            end_utc = end.astimezone(UTC).isoformat(timespec='seconds')
            changed = (datetime.fromisoformat(row['started_at']) != datetime.fromisoformat(start_utc) or
                       datetime.fromisoformat(row['ended_at']) != datetime.fromisoformat(end_utc) or
                       row['ticket_key'] != (ticket_key or None) or row['note'] != note or
                       row['title'] != (title if title is not None else row['title']))
            if not changed:
                return
            # Overlapping raw sessions cannot be represented without double-counting.
            for other in db.execute('SELECT id,started_at,ended_at FROM sessions WHERE id<>?', (session_id,)):
                other_start = datetime.fromisoformat(other['started_at'])
                other_end = datetime.fromisoformat(other['ended_at']) if other['ended_at'] else datetime.now(UTC)
                if start < other_end and end > other_start:
                    raise ValueError(f'Session overlaps recorded session #{other["id"]}; correct its timing first')
            db.execute('''UPDATE sessions SET started_at=?,ended_at=?,last_seen_at=?,ticket_key=?,note=?,title=?
                         WHERE id=?''', (start_utc, end_utc, end_utc,
                                      ticket_key or None, note, title if title is not None else row['title'], session_id))
            # Preserve its Tempo ID for a PUT, but show "Queued for update"
            # immediately instead of displaying a stale "Synced" badge.
            if linked:
                db.execute("UPDATE tempo_sync SET state='pending',error=NULL WHERE session_id=?", (session_id,))
        self.notify_change()

    def delete_session(self, session_id: int):
        with self.connection() as db:
            row = db.execute('SELECT ended_at FROM sessions WHERE id=?', (session_id,)).fetchone()
            if row and row['ended_at'] is None:
                raise ValueError('Pause the session before deleting it')
            linked = db.execute('SELECT remote_id, state FROM tempo_sync WHERE session_id=?', (session_id,)).fetchone()
            if linked and linked['state'] in ('sending', 'uncertain'):
                raise ValueError('Resolve the in-flight/uncertain Tempo result before deleting this session')
            if linked and linked['remote_id']:
                db.execute("UPDATE tempo_sync SET state='delete_pending',error=NULL WHERE session_id=?", (session_id,))
            else:
                db.execute('DELETE FROM tempo_sync WHERE session_id=?', (session_id,))
            db.execute('DELETE FROM sessions WHERE id=?', (session_id,))
        if row:
            self.notify_change()

    def sync_rows(self, *, after: str) -> list[dict]:
        """Completed ticket-linked sessions on/after the user's opt-in time."""
        with self.connection() as db:
            rows = db.execute("""SELECT s.*, t.jira_issue_id,
                    y.remote_id, y.sent_hash, y.state AS sync_state, y.error AS sync_error
                    FROM sessions s
                    LEFT JOIN tickets t ON t.issue_key=s.ticket_key
                    LEFT JOIN tempo_sync y ON y.session_id=s.id
                    WHERE s.ended_at IS NOT NULL AND (s.started_at >= ? OR y.remote_id IS NOT NULL)
                      AND (s.ticket_key IS NOT NULL OR y.remote_id IS NOT NULL)
                    ORDER BY s.started_at""", (after,)).fetchall()
            return [dict(r) for r in rows]

    def sync_detail(self, session_id: int) -> dict | None:
        with self.connection() as db:
            row = db.execute('SELECT * FROM tempo_sync WHERE session_id=?', (session_id,)).fetchone()
            return dict(row) if row else None

    def has_daily_rollups(self) -> bool:
        """Block new per-session posts until UAT daily worklogs are removed."""
        with self.connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tempo_daily'").fetchone()
            return bool(exists and db.execute('SELECT 1 FROM tempo_daily WHERE remote_id IS NOT NULL LIMIT 1').fetchone())

    def reset_tempo_uat(self):
        """Use *only after* manually deleting PunchCard test worklogs in Tempo."""
        with self.connection() as db:
            db.execute('DELETE FROM tempo_sync')
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tempo_daily'").fetchone()
            if exists:
                db.execute('DROP TABLE tempo_daily')

    def release_sync_claim(self, session_id: int, *, orphan: bool = False) -> None:
        """Undo an unsubmitted API operation when the user disables Tempo sync."""
        with self.connection() as db:
            db.execute("UPDATE tempo_sync SET state=?,error=NULL WHERE session_id=? AND state='sending'",
                       ('delete_pending' if orphan else 'pending', session_id))
        self.notify_change()

    def sync_orphans(self) -> list[dict]:
        """Tempo worklogs belonging to sessions deleted locally, pending deletion."""
        with self.connection() as db:
            rows = db.execute("""SELECT y.* FROM tempo_sync y LEFT JOIN sessions s ON s.id=y.session_id
                                 WHERE s.id IS NULL AND y.remote_id IS NOT NULL
                                 ORDER BY y.session_id""").fetchall()
            return [dict(row) for row in rows]

    def claim_delete(self, session_id: int) -> bool:
        with self.connection() as db:
            result = db.execute("""UPDATE tempo_sync SET state='sending',last_attempt_at=?,error=NULL
                                   WHERE session_id=? AND remote_id IS NOT NULL
                                     AND state NOT IN ('sending','uncertain','error')""",
                                (utcnow(), session_id))
            return result.rowcount == 1

    def complete_delete(self, session_id: int, *, orphan=False, state='too_short', reason=None):
        with self.connection() as db:
            if orphan:
                db.execute("DELETE FROM tempo_sync WHERE session_id=? AND state='sending'", (session_id,))
            else:
                db.execute("""UPDATE tempo_sync SET remote_id=NULL,sent_hash=NULL,last_synced_at=NULL,state=?,error=?
                              WHERE session_id=? AND state='sending'""", (state, reason, session_id))

    def mark_unsyncable(self, session_id: int, state: str, reason: str):
        with self.connection() as db:
            db.execute("""INSERT INTO tempo_sync(session_id,state,error) VALUES(?,?,?)
                          ON CONFLICT(session_id) DO UPDATE SET state=excluded.state,error=excluded.error
                          WHERE remote_id IS NULL AND state NOT IN ('sending','uncertain','error')""",
                       (session_id, state, reason))

    def timesheet_signature(self, day: date) -> tuple:
        """Change signature for a single local calendar day."""
        return self.timesheet_signature_range(day, day)

    def timesheet_signature_range(self, start: date, end: date) -> tuple:
        """Changes to visible sessions or their Tempo sync statuses."""
        rows = self.list_sessions_range(start, end)
        with self.connection() as db:
            state_rows = db.execute("""SELECT session_id,state,remote_id,sent_hash,last_synced_at,error
                                       FROM tempo_sync""").fetchall()
        return (tuple((r['id'], r['kind'], r['ticket_key'], r['title'], r['started_at'],
                       r['ended_at'], r['note']) for r in rows),
                tuple(tuple(x) for x in state_rows))

    def claim_sync(self, session_id: int, fingerprint: str) -> str | None:
        """Atomically mark a PUT/POST in flight before the network request."""
        with self.connection() as db:
            row = db.execute("""SELECT s.ended_at, s.ticket_key, y.remote_id, y.sent_hash, y.state
                                FROM sessions s LEFT JOIN tempo_sync y ON y.session_id=s.id
                                WHERE s.id=?""", (session_id,)).fetchone()
            if not row or not row['ended_at'] or not row['ticket_key']:
                return None
            if row['state'] in ('sending', 'uncertain', 'error') or (row['state'] == 'synced' and row['sent_hash'] == fingerprint):
                return None
            action = 'update' if row['remote_id'] else 'create'
            db.execute("""INSERT INTO tempo_sync(session_id,remote_id,sent_hash,state,last_attempt_at,error)
                          VALUES(?,?,?,'sending',?,NULL)
                          ON CONFLICT(session_id) DO UPDATE SET state='sending', last_attempt_at=excluded.last_attempt_at, error=NULL""",
                       (session_id, row['remote_id'], row['sent_hash'], utcnow()))
            return action

    def complete_sync(self, session_id: int, remote_id: str, fingerprint: str):
        with self.connection() as db:
            db.execute("""UPDATE tempo_sync SET state='synced',remote_id=?,sent_hash=?,last_synced_at=?,error=NULL
                          WHERE session_id=? AND state='sending' """, (remote_id, fingerprint, utcnow(), session_id))

    def mark_unchanged_synced(self, session_id: int, fingerprint: str):
        """Local edit did not alter Tempo payload (e.g. moved a few seconds)."""
        with self.connection() as db:
            db.execute("""UPDATE tempo_sync SET state='synced',error=NULL
                          WHERE session_id=? AND remote_id IS NOT NULL AND sent_hash=?
                          AND state='pending'""", (session_id, fingerprint))

    def fail_sync(self, session_id: int, error: str, *, uncertain: bool = False):
        with self.connection() as db:
            db.execute("""UPDATE tempo_sync SET state=?,error=? WHERE session_id=? AND state='sending' """,
                       ('uncertain' if uncertain else 'error', str(error)[:600], session_id))

    def reset_missing_remote(self, session_id: int, expected_remote_id: str) -> bool:
        """Requeue a confirmed missing worklog, without touching activity data.

        Only the expected remote ID may be reset to avoid races with another sync.
        Existing hashes are invalidated so the next sync creates a new worklog.
        """
        with self.connection() as db:
            result = db.execute("""UPDATE tempo_sync
                    SET remote_id=NULL, sent_hash=NULL, last_synced_at=NULL, state='pending', error=NULL
                    WHERE session_id=? AND remote_id=?
                      AND state NOT IN ('uncertain')""",
                    (session_id, expected_remote_id))
            return result.rowcount == 1

    def retry_failed_syncs(self):
        """Retry only known rejections, never ambiguous network outcomes."""
        with self.connection() as db:
            db.execute("UPDATE tempo_sync SET state='pending', error=NULL WHERE state='error'")

    def sync_summary(self) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT state,COUNT(*) AS n FROM tempo_sync GROUP BY state").fetchall()
            return {r['state']: r['n'] for r in rows}

