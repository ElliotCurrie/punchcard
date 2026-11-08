"""PunchCard's composition root: the first file to read to understand the app.

Application creates long-lived objects, connects their notifications, exposes
cross-component user actions, and manages startup/shutdown. Component methods
own the actual behaviour; UI classes own only presentation and selection state.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date

from fastapi import HTTPException
from fastapi.responses import Response
from nicegui import app as nicegui_app, ui

from .database import Database
from .desktop import Desktop
from .jira import Jira
from .settings import ProjectPaths, Settings
from .teams import TeamsCallMonitor
from .tempo import Tempo
from .timesheet import Timesheet
from .tracker import SessionTracker
from .ui.settings import SettingsView
from .ui.ticket_picker import TicketPicker
from .ui.timesheet import TimesheetView
from .ui.user_guide import UserGuideView
from .ui.widget import Widget

LOG = logging.getLogger(__name__)


class Application:
    """Defines what PunchCard consists of and how its objects collaborate."""

    def __init__(self, *, database_path=None, paths: ProjectPaths | None = None):
        # Application objects: authoritative data and operational state.
        self.paths = paths or ProjectPaths()
        self.database = Database(database_path or self.paths.database)
        self.settings = Settings(self.database, paths=self.paths)
        self.jira = Jira(self.settings, self.database)
        self.tempo = Tempo(self.settings, self.database)
        self.tracker = SessionTracker(self.database)
        self.timesheet = Timesheet(self.database)
        self.teams = TeamsCallMonitor(self._on_teams_event)
        self.desktop = Desktop(
            on_pause=lambda: self.tracker.pause('tray_pause'),
            on_windows_locked=self._windows_locked,
            on_windows_unlocked=self._windows_unlocked,
            paths=self.paths,
        )

        # UI objects: presentation and user intent, never business rules.
        self.widget = Widget(
            self.desktop, self.tracker, self.jira, self.settings,
            on_toggle_tracking=self.toggle_tracking,
        )
        self.ticket_picker = TicketPicker(
            self.desktop, self.jira, on_start_tracking=self.start_tracking_ticket,
        )
        self.timesheet_view = TimesheetView(
            self.desktop, self.timesheet, self.tempo, self.settings, self.jira,
        )
        self.settings_view = SettingsView(self.desktop, self.settings, self.jira, self.tempo)
        self.user_guide_view = UserGuideView(self.desktop, self.paths.user_guide)

        # The relationships between objects are all declared here.
        self.tracker.on_change.subscribe(self.widget.invalidate)
        self.tracker.on_session_completed.subscribe(self._session_completed)
        self.jira.on_change.subscribe(self.settings.enable_tempo_sync_if_ready)
        self.jira.on_change.subscribe(self.widget.invalidate)
        self.jira.on_change.subscribe(self.ticket_picker.invalidate)
        self.settings.on_change.subscribe(self.widget.invalidate)
        self.settings.on_change.subscribe(self.timesheet_view.invalidate)
        self.timesheet.on_change.subscribe(self.timesheet_view.invalidate)
        self.tempo.on_change.subscribe(self.timesheet_view.invalidate)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._heartbeat_task: asyncio.Task | None = None

    # Cross-component user actions; actual state transitions belong to objects.
    def start_tracking_ticket(self, ticket_key: str) -> bool:
        ticket = self.jira.get_ticket(ticket_key)
        if not ticket or ticket['status_category'] == 'done':
            return False
        self.tracker.start_ticket_session(ticket_key, ticket['title'])
        return True

    def toggle_tracking(self) -> bool:
        current = self.tracker.status()
        if current['current'] is not None:
            self.tracker.pause()
            return True
        last_ticket = current['last_ticket']
        return self.start_tracking_ticket(last_ticket) if last_ticket else False

    # Events express consequences; the tracker knows nothing about UI or Tempo.
    def _session_completed(self, _session: dict) -> None:
        self.timesheet.invalidate()
        self._request_tempo_sync()

    def _request_tempo_sync(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.tempo.request_synchronisation)

    def _windows_locked(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.tracker.on_windows_locked)

    def _windows_unlocked(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.tracker.on_windows_unlocked)

    def _on_teams_event(self, event, evidence) -> None:
        """Interpret Teams observations on NiceGUI's event loop, not its polling thread."""
        if self._loop is None:
            return

        def apply():
            if event == 'start':
                self.tracker.on_teams_call_started(evidence.title, evidence.as_metadata())
            elif event == 'update':
                self.tracker.on_teams_call_updated(evidence.title, evidence.as_metadata())
            elif event == 'end':
                self.tracker.on_teams_call_ended()

        self._loop.call_soon_threadsafe(apply)

    def register_routes(self) -> None:
        self.widget.register_page()
        self.ticket_picker.register_page()
        self.timesheet_view.register_page()
        self.settings_view.register_page()
        self.user_guide_view.register_page()

        @nicegui_app.get('/exports/sessions.csv')
        def export_sessions(start: date | None = None, end: date | None = None):
            """CSV export for an inclusive local date range, or all when omitted."""
            if (start is None) != (end is None) or (start and end and start > end):
                raise HTTPException(status_code=400, detail='Provide a valid start/end date range')
            filename = (f'punchcard-{start.isoformat()}-to-{end.isoformat()}.csv'
                        if start is not None else 'punchcard.csv')
            return Response(
                content=self.timesheet.export_csv(start, end),
                media_type='text/csv; charset=utf-8',
                headers={'Content-Disposition': f'attachment; filename="{filename}"',
                         'Cache-Control': 'no-store'},
            )

        @nicegui_app.get('/health')
        def health():
            return {'status': 'ok'}

        nicegui_app.on_startup(self.start)
        nicegui_app.on_shutdown(self.stop)

    async def start(self) -> None:
        """Start observers and the Tempo worker once the event loop exists."""
        self._loop = asyncio.get_running_loop()
        # SQLite's after-commit notification is a durable wake-up path. It
        # covers edits made outside Tracker, and missed wakes are retried at startup.
        self.database.set_change_callback(self._request_tempo_sync)
        self.tempo.start()
        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name='PunchCardHeartbeat')

        if os.name == 'nt':
            self.teams.start()
            self.desktop.start(self._loop)
        else:
            LOG.warning('Windows desktop integration unavailable; open the local NiceGUI URL')

    async def stop(self) -> None:
        self.database.set_change_callback(None)
        self.teams.stop()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        await self.tempo.stop()
        self.tracker.pause('application_exit')
        self.desktop.stop()
        self._loop = None

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(15)
            self.tracker.checkpoint()

    def run(self) -> None:
        logging.basicConfig(level=logging.INFO)
        self.register_routes()
        ui.run(host='127.0.0.1', port=self.desktop.web_port,
               reload=False, show=os.name != 'nt', title='PunchCard')
