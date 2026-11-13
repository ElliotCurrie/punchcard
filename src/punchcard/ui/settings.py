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
        @ui.page('/settings')
        def render_settings():
            saved = self.settings.load()
            connection_busy = {'jira': False, 'tempo': False}

            def update_action_buttons():
                for kind, save, test, disconnect in (
                    ('jira', save_jira_button, jira_test_button, jira_disconnect_button),
                    ('tempo', save_tempo_button, tempo_test_button, tempo_disconnect_button),
                ):
                    busy = connection_busy[kind]
                    configured = self.settings.connection_status(kind)['configured']
                    save.disable() if busy else save.enable()
                    for button in (test, disconnect):
                        button.disable() if busy or not configured else button.enable()

            def update_indicator(kind, badge, detail_label):
                status = self.settings.connection_status(kind)
                badge.set_text(status['label'])
                badge.props(f"color={status['color']}")

                # The badge communicates successful verification; retain useful error details.
                detail = '' if status['state'] == 'connected' else status['detail']
                detail_label.set_text(detail)
                detail_label.set_visibility(bool(detail))

                if kind == 'tempo':
                    sync_message = self.settings.tempo_sync_status()
                    if status['state'] == 'connected':
                        sync_message = sync_message.removeprefix('Tempo connected. ')
                    tempo_sync_detail.set_text(sync_message)

            async def test_jira(*, notify=True):
                connection_busy['jira'] = True
                update_action_buttons()
                try:
                    await asyncio.to_thread(self.jira.check_connection)
                except Exception as exc:
                    if notify:
                        ui.notify(f'Jira connection failed: {exc}', type='negative')
                else:
                    if notify:
                        ui.notify('Jira connected', type='positive')
                finally:
                    connection_busy['jira'] = False
                    update_indicator('jira', jira_badge, jira_connection_detail)
                    update_action_buttons()

            async def test_tempo(*, notify=True):
                connection_busy['tempo'] = True
                update_action_buttons()
                try:
                    await asyncio.to_thread(self.tempo.check_connection)
                except Exception as exc:
                    if notify:
                        ui.notify(f'Tempo connection failed: {exc}', type='negative')
                else:
                    if notify:
                        ui.notify(self.settings.tempo_sync_status(), type='positive')
                finally:
                    connection_busy['tempo'] = False
                    update_indicator('tempo', tempo_badge, tempo_connection_detail)
                    update_action_buttons()

            with window_layout(self.desktop, 'settings', 'PUNCHCARD / SETTINGS', padding=22, scroll_area=True):
                # Jira connection
                with ui.expansion('', value=False, group='connections').classes('w-full pc-settings-expansion') as jira_section:
                    with jira_section.add_slot('header'):
                        with ui.row().classes('w-full items-center no-wrap gap-2'):
                            ui.icon('link')
                            ui.label('Jira connection')
                            ui.space()
                            jira_badge = ui.badge('Disconnected', color='grey-6').classes('q-mr-md')

                    with ui.column().classes('w-full gap-3 pt-2'):
                        url_input = ui.input('Jira Cloud URL', value=saved.get('jira_url', ''),
                                             placeholder='https://company.atlassian.net').classes('w-full')
                        email_input = ui.input('Jira account email', value=saved.get('jira_email', '')).classes('w-full')
                        token_input = ui.input('Jira API token (blank keeps current)', password=True,
                                               password_toggle_button=True).classes('w-full')
                        ui.label('API token: read:jira-work').classes('text-caption text-grey-7')
                        ui.label('Jira: Browse Projects').classes('text-caption text-grey-7')
                        jira_expiry = ui.input('Token expiry date', value=self.settings.token_expiry_value('jira')).props('type=date').classes('w-full')
                        jira_expiry_label = ui.label('Token · ' + self.settings.expiry_status('jira')['text']).classes('text-caption text-grey-7')
                        jira_connection_detail = ui.label('').classes('text-caption text-grey-7')
                        update_indicator('jira', jira_badge, jira_connection_detail)
                        jira_fetch_status = ui.label('').classes('text-caption text-grey-7')
                        jira_fetch_status.set_visibility(False)

                        async def save_jira():
                            connection_busy['jira'] = True
                            update_action_buttons()
                            try:
                                self.settings.save_jira_connection(
                                    url_input.value or '', email_input.value or '', token_input.value or '',
                                    token_expiry_date=jira_expiry.value,
                                )
                            except Exception as exc:
                                ui.notify(str(exc), type='negative')
                                connection_busy['jira'] = False
                                update_action_buttons()
                                return

                            token_input.set_value('')
                            update_indicator('jira', jira_badge, jira_connection_detail)
                            jira_expiry_label.set_text('Token · ' + self.settings.expiry_status('jira')['text'])
                            jira_fetch_status.set_text('Fetching Jira issues…')
                            jira_fetch_status.set_visibility(True)
                            try:
                                issues = await asyncio.to_thread(self.jira.refresh_tickets)
                            except Exception as exc:
                                jira_fetch_status.set_text('Jira saved; issue fetch failed. Refresh from the issue picker.')
                                ui.notify(f'Jira saved, but issues could not be fetched: {exc}', type='warning')
                            else:
                                count = sum(bool(t['assigned']) for t in issues)
                                jira_fetch_status.set_text(f'{count} unfinished assigned issues fetched')
                                ui.notify(f'Jira connected · {count} assigned issues loaded', type='positive')
                                jira_section.value = False
                                tempo_section.value = True
                            finally:
                                connection_busy['jira'] = False
                                update_indicator('jira', jira_badge, jira_connection_detail)
                                update_action_buttons()

                        with ui.row().classes('w-full items-center justify-between gap-2'):
                            with ui.row().classes('items-center gap-2'):
                                save_jira_button = ui.button('Save', on_click=save_jira)
                                jira_test_button = ui.button('Test connection',
                                                             on_click=lambda: test_jira()).props('flat dense')
                            jira_disconnect_button = ui.button('Disconnect',
                                on_click=lambda: confirm_disconnect('jira')).props('flat dense color=negative')

                # Tempo connection
                with ui.expansion('', value=False, group='connections').classes('w-full pc-settings-expansion') as tempo_section:
                    with tempo_section.add_slot('header'):
                        with ui.row().classes('w-full items-center no-wrap gap-2'):
                            ui.icon('sync')
                            ui.label('Tempo synchronisation')
                            ui.space()
                            tempo_badge = ui.badge('Disconnected', color='grey-6').classes('q-mr-md')

                    with ui.column().classes('w-full gap-3 pt-2'):
                        tempo_token = ui.input('Tempo API token (blank keeps current)', password=True,
                                               password_toggle_button=True).classes('w-full')
                        ui.label('API token: Worklogs → Manage').classes('text-caption text-grey-7')
                        tempo_expiry = ui.input('Token expiry date', value=self.settings.token_expiry_value('tempo')).props('type=date').classes('w-full')
                        tempo_expiry_label = ui.label('Token · ' + self.settings.expiry_status('tempo')['text']).classes('text-caption text-grey-7')
                        tempo_connection_detail = ui.label('').classes('text-caption text-grey-7')
                        tempo_sync_detail = ui.label('').classes('text-caption text-grey-7')
                        update_indicator('tempo', tempo_badge, tempo_connection_detail)

                        async def save_tempo():
                            connection_busy['tempo'] = True
                            update_action_buttons()
                            try:
                                await asyncio.to_thread(
                                    self.tempo.save_connection,
                                    token=tempo_token.value or '', token_expiry_date=tempo_expiry.value,
                                    api_url=self.settings.load().get('tempo_api_url') or 'https://api.tempo.io',
                                )
                            except Exception as exc:
                                ui.notify(f'Tempo connection failed: {exc}', type='negative')
                            else:
                                tempo_token.set_value('')
                                tempo_expiry_label.set_text('Token · ' + self.settings.expiry_status('tempo')['text'])
                                self.tempo.retry_failed_syncs()
                                ui.notify(self.settings.tempo_sync_status(), type='positive')
                                tempo_section.value = False
                            finally:
                                connection_busy['tempo'] = False
                                update_indicator('tempo', tempo_badge, tempo_connection_detail)
                                update_action_buttons()

                        with ui.row().classes('w-full items-center justify-between gap-2'):
                            with ui.row().classes('items-center gap-2'):
                                save_tempo_button = ui.button('Save', on_click=save_tempo)
                                tempo_test_button = ui.button('Test connection',
                                                              on_click=lambda: test_tempo()).props('flat dense')
                            tempo_disconnect_button = ui.button('Disconnect',
                                on_click=lambda: confirm_disconnect('tempo')).props('flat dense color=negative')

                def confirm_disconnect(kind):
                    if connection_busy[kind] or not self.settings.connection_status(kind)['configured']:
                        return
                    name = 'Jira' if kind == 'jira' else 'Tempo'
                    description = (
                        'This removes the Jira credentials and cached issue selection, and disables Tempo sync '
                        'until Jira is reconnected. Recorded sessions are kept.' if kind == 'jira' else
                        'This removes the saved Tempo token and disables synchronisation. Existing sessions '
                        'and remote Tempo worklogs will not be deleted.'
                    )
                    with ui.dialog() as dialog, ui.card().classes('w-96 max-w-full gap-3'):
                        ui.label(f'Disconnect {name}?').classes('text-h6')
                        ui.label(description).classes('text-body2 text-grey-7')

                        def disconnect():
                            if connection_busy[kind]:
                                return
                            connection_busy[kind] = True
                            update_action_buttons()
                            try:
                                if kind == 'jira':
                                    self.jira.disconnect()
                                    url_input.set_value('')
                                    email_input.set_value('')
                                    token_input.set_value('')
                                    jira_expiry.set_value('')
                                    jira_expiry_label.set_text('Token · Not configured')
                                    jira_fetch_status.set_visibility(False)
                                else:
                                    self.settings.disconnect_tempo()
                                    tempo_token.set_value('')
                                    tempo_expiry.set_value('')
                                    tempo_expiry_label.set_text('Token · Not configured')
                            except Exception as exc:
                                ui.notify(f'Could not disconnect {name}: {exc}', type='negative')
                                return
                            finally:
                                connection_busy[kind] = False
                                update_indicator('jira', jira_badge, jira_connection_detail)
                                update_indicator('tempo', tempo_badge, tempo_connection_detail)
                                update_action_buttons()
                            dialog.close()
                            ui.notify(f'{name} disconnected', type='positive')

                        with ui.row().classes('w-full justify-end gap-2'):
                            ui.button('Cancel', on_click=dialog.close).props('flat')
                            ui.button('Disconnect', color='negative', on_click=disconnect)
                    dialog.open()

                async def check_existing_connections():
                    checks = []
                    if self.settings.connection_status('jira')['configured']:
                        checks.append(test_jira(notify=False))
                    if self.settings.connection_status('tempo')['configured']:
                        checks.append(test_tempo(notify=False))
                    if checks:
                        await asyncio.gather(*checks)

                def refresh_indicators():
                    update_indicator('jira', jira_badge, jira_connection_detail)
                    update_indicator('tempo', tempo_badge, tempo_connection_detail)
                    update_action_buttons()

                update_action_buttons()
                ui.timer(0.3, check_existing_connections, once=True)
                ui.timer(2.0, refresh_indicators)
