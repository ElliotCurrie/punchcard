"""Tracking UI: reads Tracker state and forwards user intents to Application."""
import asyncio
from nicegui import ui
from .layout import window_layout, open_view, format_elapsed

class Widget:
    def __init__(self, desktop, tracker, jira, settings, *, on_toggle_tracking):
        self.desktop = desktop
        self.tracker = tracker
        self.jira = jira
        self.settings = settings
        self.on_toggle_tracking = on_toggle_tracking
        self._revision = 0

    def invalidate(self, *_args) -> None:
        """Record a new view revision without touching NiceGUI from a worker thread."""
        self._revision += 1

    def register_page(self) -> None:
        # Each NiceGUI route builds controls for its own browser connection.
        @ui.page('/')
        def render_widget():
            with window_layout(self.desktop, 'widget', 'PUNCHCARD', padding=12, status=True) as status_badge:

                with ui.row().classes('w-full items-center justify-between'):
                    ui.label('Working on').classes('text-caption text-grey-7')

                    async def choose_ticket():
                        # Use the locally cached hierarchy whenever possible.
                        # Manual refresh now lives in the ticket picker itself.
                        if self.jira.list_tickets():
                            open_view(self.desktop, 'tickets', reload=True)
                            return
                        choose_button.disable()
                        choose_button.props('loading')
                        try:
                            issues = await asyncio.to_thread(self.jira.refresh_tickets)
                            assigned = sum(bool(t['assigned']) for t in issues)
                            if not assigned:
                                ui.notify('No unfinished assigned tickets found', type='warning')
                            open_view(self.desktop, 'tickets', reload=True)
                        except Exception as exc:
                            ui.notify(f'Could not fetch Jira tickets: {exc}', type='negative')
                        finally:
                            choose_button.props(remove='loading')
                            choose_button.enable()

                    choose_button = ui.button(icon='account_tree',
                                              on_click=choose_ticket).props('dense outline round size=sm').tooltip('Choose ticket')
                current_ticket_label = ui.label('Select a Jira ticket').classes(
                    'font-medium text-blue-grey-9 w-full text-ellipsis overflow-hidden')

                time_label = ui.label('00:00:00').classes('text-3xl font-semibold text-blue-grey-9').style(
                    'font-family: "Segoe UI", Arial, sans-serif; font-variant-numeric: tabular-nums; letter-spacing: 0;')
                details_label = ui.label('Not tracking').classes('text-xs text-grey-7')

                def refresh_tracking_display():
                    s = self.tracker.status()
                    active = s['current']
                    is_tracking = active is not None
                    if button_state['tracking'] != is_tracking:
                        track_button.set_text('Stop' if is_tracking else 'Start')
                        track_button.set_icon('stop' if is_tracking else 'play_arrow')
                        track_button.props('color=negative' if is_tracking else 'color=positive')
                        button_state['tracking'] = is_tracking
                    if active:
                        if active['kind'] == 'ticket':
                            current_ticket_label.set_text(f"{active['ticket_key']} · {active['title']}")
                        else:
                            current_ticket_label.set_text('Teams call')
                        time_label.set_text(format_elapsed(s['elapsed_seconds']))
                        status_badge.set_text('Tracking' if active['kind'] == 'ticket' else 'Meeting')
                        status_badge.props('color=positive' if active['kind'] == 'ticket' else 'color=orange')
                        details_label.set_text(active['title'][:60])
                    else:
                        last = next((t for t in self.jira.list_tickets()
                                     if t['issue_key'] == s['last_ticket']), None)
                        current_ticket_label.set_text(
                            f"{last['issue_key']} · {last['title']}" if last else 'Select a Jira ticket')
                        time_label.set_text('00:00:00')
                        status_badge.set_text('Paused')
                        status_badge.props('color=grey')
                        details_label.set_text('Choose a ticket or resume tracking')

                def pause_resume():
                    if not self.on_toggle_tracking():
                        ui.notify('Choose an unfinished Jira ticket first', color='warning')
                    refresh_tracking_display()

                button_state = {'tracking': None}
                with ui.row().classes('gap-2 w-full'):
                    track_button = ui.button('Start', on_click=pause_resume, icon='play_arrow',
                                             color='positive').props('dense unelevated').classes('flex-1')

                with ui.row().classes('items-center w-full justify-between no-wrap'):
                    ui.button(icon='list_alt', on_click=lambda: open_view(self.desktop, 'timesheet')).props(
                        'flat dense round').tooltip('Timesheet')
                    ui.button(icon='settings', on_click=lambda: open_view(self.desktop, 'settings', reload=True)).props(
                        'flat dense round').tooltip('Settings')
                    ui.button(icon='help_outline', on_click=lambda: open_view(self.desktop, 'guide')).props(
                        'flat dense round').tooltip('User guide')

                # Only show expiry notices on the compact widget when action is near.
                expiry_warning = ui.label('').classes('text-xs text-orange-9 w-full')
                def refresh_expiry_warning():
                    warnings = self.settings.expiring_tokens()
                    expiry_warning.set_text('API token: ' + ' | '.join(warnings) if warnings else '')
                    expiry_warning.set_visibility(bool(warnings))

                refresh_tracking_display()
                refresh_expiry_warning()
                change_seen = {'revision': self._revision}

                def refresh_if_changed():
                    if change_seen['revision'] != self._revision:
                        change_seen['revision'] = self._revision
                        refresh_tracking_display()
                        refresh_expiry_warning()

                ui.timer(0.25, refresh_if_changed)
                ui.timer(1.0, refresh_tracking_display)  # Running clock
                ui.timer(60.0, refresh_expiry_warning)
