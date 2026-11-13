"""NiceGUI timesheet: daily browsing and date-range export.

Timesheet owns historical sessions and edits; this view owns period selection,
controls and rendering. Each NiceGUI client builds its own view-local state.
"""
import os
from datetime import date, datetime, timedelta

from nicegui import ui

from ..timestamps import edited_local_timestamp, local_datetime
from .layout import format_timesheet_total, window_layout


class TimesheetView:
    def __init__(self, desktop, timesheet, tempo, settings, jira):
        self.desktop = desktop
        self.timesheet = timesheet
        self.tempo = tempo
        self.settings = settings
        self.jira = jira
        self._revision = 0

    def invalidate(self, *_args) -> None:
        """A dependency changed; each browser view refreshes on its own loop."""
        self._revision += 1

    def register_page(self) -> None:
        @ui.page('/timesheet')
        def render_timesheet():
            with window_layout(self.desktop, 'timesheet', 'PUNCHCARD / TIMESHEET', padding=22):
                # View state stays local to this browser connection, not on Timesheet.
                with ui.row().classes('items-center gap-2 w-full flex-wrap'):
                    ui.button(icon='chevron_left', on_click=lambda: navigate(-1)).props(
                        'flat dense round').tooltip('Previous day')
                    ui.button('Today', on_click=lambda: jump_to_today()).props('flat dense')
                    ui.button(icon='chevron_right', on_click=lambda: navigate(1)).props(
                        'flat dense round').tooltip('Next day')
                    selected_day = ui.input('Date', value=date.today().isoformat()).props(
                        'type=date dense').classes('w-40')
                    ui.space()
                    ui.button(icon='file_download', on_click=lambda: export_dialog()).props(
                        'outline dense round').tooltip('Export CSV')
                period_label = ui.label('').classes('text-caption text-grey-7')

                def selected_date() -> date | None:
                    try:
                        return date.fromisoformat(selected_day.value)
                    except (TypeError, ValueError):
                        return None

                def navigate(direction: int):
                    day = selected_date() or date.today()
                    selected_day.set_value((day + timedelta(days=direction)).isoformat())
                    update_live_timesheet()

                def jump_to_today():
                    selected_day.set_value(date.today().isoformat())
                    update_live_timesheet()

                def export_dialog():
                    day = selected_date()
                    if day is None:
                        ui.notify('Select a valid date before exporting', type='warning')
                        return
                    with ui.dialog() as dialog, ui.card().classes('w-96 max-w-full gap-3'):
                        ui.label('Export timesheet').classes('text-h6')
                        ui.label('Include sessions overlapping this date range.').classes(
                            'text-caption text-grey-7')
                        start_input = ui.input('From', value=day.isoformat()).props(
                            'type=date').classes('w-full')
                        end_input = ui.input('To', value=day.isoformat()).props(
                            'type=date').classes('w-full')

                        def download_range():
                            try:
                                start = date.fromisoformat(start_input.value)
                                end = date.fromisoformat(end_input.value)
                                if start > end:
                                    raise ValueError('The end date must be on or after the start date')
                                if not self.timesheet.sessions_in_range(start, end):
                                    ui.notify('No sessions in the selected range', type='warning')
                                    return
                                filename = f'punchcard-{start.isoformat()}-to-{end.isoformat()}.csv'
                                if os.name == 'nt':
                                    self.desktop.command(
                                        'export_csv', window='timesheet',
                                        start_date=start.isoformat(), end_date=end.isoformat(),
                                    )
                                else:
                                    ui.download.content(self.timesheet.export_csv(start, end), filename, 'text/csv')
                            except (ValueError, TypeError) as exc:
                                ui.notify(str(exc) or 'Choose valid dates', type='negative')
                                return
                            dialog.close()

                        with ui.row().classes('justify-end w-full'):
                            ui.button('Cancel', on_click=dialog.close).props('flat')
                            ui.button('Export', on_click=download_range)
                    dialog.open()

                def toggle_tempo(event):
                    requested = bool(event.value)
                    current = bool(self.settings.load().get('tempo_sync_enabled', False))
                    if requested == current:
                        return
                    try:
                        self.settings.set_tempo_sync_enabled(requested)
                    except Exception as exc:
                        tempo_enabled.set_value(current)
                        ui.notify(str(exc), type='warning')
                        return
                    tempo_status_label.set_text(self.settings.tempo_sync_status())
                    render_sessions.refresh()

                with ui.row().classes('w-full items-center justify-between no-wrap gap-2'):
                    total_label = ui.label('').classes('text-caption')
                    tempo_enabled = ui.switch(
                        'Tempo sync',
                        value=bool(self.settings.load().get('tempo_sync_enabled', False)),
                        on_change=toggle_tempo,
                    ).props('dense size=sm').classes('pc-tempo-toggle')
                    tempo_enabled.tooltip('Sync completed sessions automatically')
                tempo_status_label = ui.label(self.settings.tempo_sync_status()).classes(
                    'text-caption text-grey-7')

                # The signature is checked periodically to catch background sync and
                # tracker writes. Live totals update without recreating edit dialogs.
                signature_state = {'day': None, 'value': None, 'revision': self._revision}
                displayed = {'rows': [], 'day': None}

                def render_session(row, *, tempo_enabled: bool, sync_cutoff: str):
                    label = row['ticket_key'] or row['kind'].title()
                    sync = self.timesheet.sync_detail(row['id'])
                    status = self.tempo.describe_session_sync(
                        row, sync, enabled=tempo_enabled, cutoff=sync_cutoff,
                    )
                    with ui.card().classes('w-full gap-2'):
                        with ui.row().classes('w-full items-start justify-between no-wrap'):
                            ui.label(f'{label} — {row["title"]}').classes(
                                'font-medium flex-1 min-w-0 break-words',
                            )
                            badge = ui.badge(status['text'], color=status['color']).classes('shrink-0')
                            if status.get('detail'):
                                badge.tooltip(status['detail'])
                        if row['note']:
                            ui.label(row['note']).classes('text-caption text-grey-7')
                        with ui.row().classes('w-full items-center justify-between no-wrap'):
                            ui.label(self.tempo.format_session_minutes(row)).classes(
                                'text-caption text-grey-7',
                            )
                            with ui.row().classes('items-center gap-1'):
                                ui.button(icon='edit', on_click=lambda r=row: edit_dialog(r)).props(
                                    'flat dense round').tooltip('Edit session')
                                ui.button(icon='delete', on_click=lambda r=row: delete_dialog(r)).props(
                                    'flat dense round color=negative').tooltip('Delete session')

                @ui.refreshable
                def render_sessions():
                    day = selected_date()
                    displayed['day'] = day
                    if day is None:
                        period_label.set_text('')
                        total_label.set_text('Select a valid date')
                        return
                    period_label.set_text(f'{day:%A %d %B %Y}')
                    rows = self.timesheet.load_day(day)
                    displayed['rows'] = rows
                    sync_enabled = bool(self.settings.load().get('tempo_sync_enabled', False))
                    sync_cutoff = self.timesheet.sync_cutoff()
                    if self.timesheet.has_legacy_tempo_rollups():
                        ui.label('Old Tempo test worklogs need clearing before sync can resume.').classes(
                            'text-caption text-negative')
                    if not rows:
                        ui.label('No recorded activity for this date.').classes('text-grey-6')
                    for row in rows:
                        render_session(row, tempo_enabled=sync_enabled, sync_cutoff=sync_cutoff)
                    refresh_totals()

                def refresh_totals():
                    day = displayed['day']
                    if day is None:
                        return
                    now = datetime.now().astimezone()
                    rows = displayed['rows']
                    elapsed = sum(self.timesheet.session_seconds_in_period(
                        row, day, day, now=now) for row in rows)
                    total_label.set_text(
                        f'{len(rows)} sessions · {format_timesheet_total(elapsed)} this day'
                    )

                def edit_dialog(row):
                    if not row['ended_at']:
                        ui.notify('Pause the active session before editing', color='warning')
                        return
                    with ui.dialog() as dialog, ui.card().classes('w-96'):
                        ui.label('Edit session').classes('text-h6')
                        session_title = ui.input('Activity title', value=row['title']).classes('w-full')
                        start = ui.input('Start', value=local_datetime(row['started_at']).strftime(
                            '%Y-%m-%dT%H:%M')).props('type=datetime-local').classes('w-full')
                        end = ui.input('End', value=local_datetime(row['ended_at']).strftime(
                            '%Y-%m-%dT%H:%M')).props('type=datetime-local').classes('w-full')
                        opts = {'': 'Unassigned'} | {
                            t['issue_key']: t['issue_key'] for t in self.jira.list_tickets()}
                        if row['ticket_key'] and row['ticket_key'] not in opts:
                            opts[row['ticket_key']] = row['ticket_key']
                        issue = ui.select(opts, value=row['ticket_key'] or '',
                                          label='Associate with Jira issue').classes('w-full')
                        note = ui.textarea('Notes', value=row['note']).classes('w-full')

                        def save_edit():
                            try:
                                self.timesheet.edit_session(
                                    row['id'],
                                    started_at=edited_local_timestamp(start.value, row['started_at']),
                                    ended_at=edited_local_timestamp(end.value, row['ended_at']),
                                    ticket_key=issue.value, note=note.value or '',
                                    title=session_title.value or row['title'],
                                )
                            except Exception as exc:
                                ui.notify(str(exc), type='negative')
                                return
                            dialog.close()
                            render_sessions.refresh()

                        with ui.row():
                            ui.button('Save changes', on_click=save_edit)
                            ui.button('Cancel', on_click=dialog.close).props('flat')
                    dialog.open()

                def delete_dialog(row):
                    with ui.dialog() as dialog, ui.card():
                        ui.label('Delete this session?')

                        def do_delete():
                            try:
                                self.timesheet.delete_session(row['id'])
                            except Exception as exc:
                                ui.notify(str(exc), type='negative')
                                return
                            dialog.close()
                            render_sessions.refresh()

                        with ui.row():
                            ui.button('Delete', color='negative', on_click=do_delete)
                            ui.button('Cancel', on_click=dialog.close).props('flat')
                    dialog.open()

                def update_live_timesheet():
                    stored_enabled = bool(self.settings.load().get('tempo_sync_enabled', False))
                    if bool(tempo_enabled.value) != stored_enabled:
                        tempo_enabled.set_value(stored_enabled)
                    status_text = self.settings.tempo_sync_status()
                    if tempo_status_label.text != status_text:
                        tempo_status_label.set_text(status_text)
                    day = selected_date()
                    if day is None:
                        return
                    signature = (
                        self.timesheet.day_signature(day),
                        bool(self.settings.load().get('tempo_sync_enabled', False)),
                        self.timesheet.sync_cutoff(),
                    )
                    if (signature_state['day'] != day
                            or signature_state['value'] != signature
                            or signature_state['revision'] != self._revision):
                        signature_state.update({
                            'day': day, 'value': signature, 'revision': self._revision,
                        })
                        render_sessions.refresh()
                    else:
                        refresh_totals()

                render_sessions()
                selected_day.on_value_change(lambda _: update_live_timesheet())
                ui.timer(1.0, update_live_timesheet)
