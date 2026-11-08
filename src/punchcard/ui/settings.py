"""Settings form and user interactions; Settings owns persisted configuration."""
import asyncio
from nicegui import ui
from .layout import window_layout

class SettingsView:
    def __init__(self, desktop, settings, jira, tempo):
        self.desktop = desktop
        self.settings = settings
        self.jira = jira
        self.tempo = tempo

    def register_page(self) -> None:
        # Each NiceGUI route builds controls for its own browser connection.
        @ui.page('/settings')
        def render_settings():
            saved = self.settings.load()
            with window_layout(self.desktop, 'settings', 'PUNCHCARD / SETTINGS', padding=22, scroll_area=True):

                with ui.expansion('Jira connection', icon='link', value=False).classes('w-full pc-settings-expansion') as jira_section:
                    with ui.column().classes('w-full gap-3 pt-2'):
                        url_input = ui.input('Jira Cloud URL', value=saved.get('jira_url', ''),
                                             placeholder='https://company.atlassian.net').classes('w-full')
                        email_input = ui.input('Jira account email', value=saved.get('jira_email', '')).classes('w-full')
                        token_input = ui.input('Jira API token (blank keeps current)', password=True,
                                               password_toggle_button=True).classes('w-full')
                        ui.label('API token: read:jira-work').classes('text-caption text-grey-7')
                        ui.label('Jira: Browse Projects').classes('text-caption text-grey-7')
                        jira_expiry = ui.input('Token expiry date',
                                               value=self.settings.token_expiry_value('jira')).props(
                            'type=date').classes('w-full')
                        jira_expiry_label = ui.label('Token · ' + self.settings.expiry_status('jira')['text']).classes(
                            'text-caption text-grey-7')
                        jira_fetch_status = ui.label('').classes('text-caption text-grey-7')
                        jira_fetch_status.set_visibility(False)

                        async def save_jira():
                            save_jira_button.disable()
                            try:
                                self.settings.save_jira_connection(url_input.value or '', email_input.value or '', token_input.value or '',
                                              token_expiry_date=jira_expiry.value)
                            except Exception as exc:
                                ui.notify(str(exc), type='negative')
                                save_jira_button.enable()
                                return
                            token_input.set_value('')
                            jira_expiry_label.set_text('Token · ' + self.settings.expiry_status('jira')['text'])
                            jira_fetch_status.set_text('Fetching Jira tickets…')
                            jira_fetch_status.set_visibility(True)
                            try:
                                issues = await asyncio.to_thread(self.jira.refresh_tickets)
                            except Exception as exc:
                                # Credentials are already saved; ticket fetch is recoverable in the picker.
                                jira_fetch_status.set_text('Jira saved; ticket fetch failed. Refresh from the ticket picker.')
                                ui.notify(f'Jira saved, but tickets could not be fetched: {exc}', type='warning')
                            else:
                                count = sum(bool(t['assigned']) for t in issues)
                                jira_fetch_status.set_text(f'{count} unfinished assigned tickets fetched')
                                ui.notify('Jira connected' if count else 'Jira saved; no assigned tickets found',
                                          type='positive' if count else 'warning')
                                jira_section.value = False
                                tempo_section.value = True
                            finally:
                                save_jira_button.enable()

                        save_jira_button = ui.button('Save', icon='save', on_click=save_jira)

                with ui.expansion('Tempo synchronisation', icon='sync', value=False).classes('w-full pc-settings-expansion') as tempo_section:
                    with ui.column().classes('w-full gap-3 pt-2'):
                        tempo_token = ui.input('Tempo API token (blank keeps current)', password=True,
                                               password_toggle_button=True).classes('w-full')
                        ui.label('API token: Worklogs → Manage').classes('text-caption text-grey-7')
                        tempo_expiry = ui.input('Token expiry date',
                                                value=self.settings.token_expiry_value('tempo')).props(
                            'type=date').classes('w-full')
                        tempo_expiry_label = ui.label('Token · ' + self.settings.expiry_status('tempo')['text']).classes(
                            'text-caption text-grey-7')
                        tempo_api = ui.input('Tempo API endpoint',
                                              value=saved.get('tempo_api_url', 'https://api.tempo.io')).classes('w-full')

                        def save_tempo():
                            try:
                                # Saving credentials must not accidentally re-enable a switch the
                                # user just turned off. The switch already saves immediately.
                                self.settings.save_tempo_connection(token=tempo_token.value or '',
                                                    token_expiry_date=tempo_expiry.value,
                                                    api_url=tempo_api.value or 'https://api.tempo.io',
                                                    enabled=bool(self.settings.load().get('tempo_sync_enabled', False)))
                            except Exception as exc:
                                ui.notify(f'Tempo setup: {exc}', type='negative')
                                return
                            tempo_token.set_value('')
                            tempo_expiry_label.set_text('Token · ' + self.settings.expiry_status('tempo')['text'])
                            self.tempo.retry_failed_syncs()
                            ui.notify('Tempo settings saved', type='positive')

                        ui.button('Save', icon='save', on_click=save_tempo)
