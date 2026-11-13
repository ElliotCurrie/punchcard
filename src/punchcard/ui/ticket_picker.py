"""Issue selection UI, separate from Jira issue data and tracking state."""
import asyncio
from nicegui import ui
from .layout import window_layout

class TicketPicker:
    def __init__(self, desktop, jira, *, on_start_tracking):
        self.desktop = desktop
        self.jira = jira
        self.on_start_tracking = on_start_tracking
        self.selected_ticket_key: str | None = None
        self._revision = 0

    def invalidate(self, *_args) -> None:
        self._revision += 1

    def select_ticket(self, issue_key: str) -> bool:
        """Selection is UI state; it need not start tracking an issue."""
        ticket = self.jira.get_ticket(issue_key)
        if not ticket or ticket['status_category'] == 'done':
            return False
        self.selected_ticket_key = issue_key
        return True

    def start_tracking_selected(self) -> bool:
        if self.selected_ticket_key is None:
            return False
        return bool(self.on_start_tracking(self.selected_ticket_key))

    def register_page(self) -> None:
        # Each NiceGUI route builds controls for its own browser connection.
        @ui.page('/tickets')
        def render_ticket_picker():
            """Hierarchy picker; explicitly refresh Jira data from here."""
            ui.add_css('.q-tree__node-header{min-height:36px;}')
            with window_layout(self.desktop, 'tickets', 'PUNCHCARD / ISSUES', padding=18):
                with ui.row().classes('items-center justify-between w-full'):
                    ui.label('Select Jira issue').classes('text-h6')

                    async def refresh_tickets():
                        refresh_button.disable()
                        try:
                            latest = await asyncio.to_thread(self.jira.refresh_tickets)
                            render_tree.refresh()
                            ui.notify(f'{sum(bool(t["assigned"]) for t in latest)} unfinished assigned issues fetched',
                                      type='positive')
                        except Exception as exc:
                            ui.notify(f'Could not refresh Jira issues: {exc}', type='negative')
                        finally:
                            refresh_button.enable()

                    refresh_button = ui.button(icon='refresh', on_click=refresh_tickets).props('flat dense round')
                ui.label('Unfinished assigned issues grouped under their Jira parents. '
                         'Unassigned parents are shown for context.').classes('text-caption text-grey-7')

                @ui.refreshable
                def render_tree():
                    ticket_list = self.jira.list_tickets()
                    items = {t['issue_key']: t for t in ticket_list}

                    if not ticket_list:
                        ui.label('No Jira issues available. Click Refresh to fetch them.').classes('text-grey-7')
                        return

                    def select_issue(e):
                        key = e.value
                        if not key:
                            return
                        item = items.get(key)
                        if not item:
                            return
                        if item['status_category'] == 'done':
                            ui.notify('This Jira issue is completed', color='warning')
                            tree.deselect()
                            return
                        if not self.select_ticket(key) or not self.start_tracking_selected():
                            ui.notify('This Jira issue is no longer selectable', type='warning')
                            return
                        ui.notify(f'Selected {key}', color='positive')
                        self.desktop.command('hide', window='tickets')

                    search = ui.input('Filter issues', placeholder='Key or summary').props(
                        'outlined dense clearable').classes('w-full')
                    tree = ui.tree(self._build_issue_tree(ticket_list),
                                   on_select=select_issue).classes('w-full')
                    tree.props('dense')
                    tree.add_slot('default-header', """
                        <div class="row items-center no-wrap" style="gap:8px;min-width:0;max-width:100%;"
                             :style="props.node.done ? 'opacity:0.5' : ''">
                            <img v-if="props.node.icon_url" :src="props.node.icon_url"
                                 @error="props.node.icon_url = ''"
                                 style="height:17px;width:17px;object-fit:contain;flex-shrink:0" />
                            <q-icon v-else :name="props.node.fallback_icon" size="17px" color="blue-grey-7" />
                            <div class="ellipsis" :title="props.node.label">
                                <strong>{{ props.node.key }}</strong> · {{ props.node.title }}
                            </div>
                            <q-badge v-if="props.node.done" color="grey-6" label="Done" />
                            <q-badge v-else-if="props.node.context" color="blue-grey-3"
                                     text-color="blue-grey-9" label="Parent" />
                        </div>
                    """)
                    tree.expand()
                    search.bind_value_to(tree, 'filter')
                    ui.label('Select any unfinished issue to start tracking.').classes(
                        'text-caption text-grey-7')

                render_tree()
                revision = {'value': self._revision}

                def refresh_changed_tickets():
                    if revision['value'] != self._revision:
                        revision['value'] = self._revision
                        render_tree.refresh()

                ui.timer(1.0, refresh_changed_tickets)

    @staticmethod
    def _fallback_icon(issue_type: str) -> str:
        """Quasar/Material icon used when Jira's own issue-type icon is unavailable."""
        name = issue_type.casefold()
        if 'sub' in name and 'task' in name:
            return 'subdirectory_arrow_right'
        if 'bug' in name or 'defect' in name:
            return 'bug_report'
        if 'epic' in name:
            return 'layers'
        if 'story' in name:
            return 'bookmark'
        if 'task' in name:
            return 'task_alt'
        return 'assignment'

    @classmethod
    def _build_issue_tree(cls, tickets: list[dict]) -> list[dict]:
        """Create a sorted tree. Done ancestors are visible but disabled."""
        items = {t['issue_key']: t for t in tickets}
        nodes = {}
        for key, item in items.items():
            done = item.get('status_category') == 'done'
            nodes[key] = {
                'id': key,
                'key': key,
                'label': f"{key} · {item['title']}",
                'title': item['title'],
                'type': item.get('issue_type') or 'Issue',
                'status': item.get('status') or '',
                'icon_url': item.get('issue_type_icon') or '',
                'fallback_icon': cls._fallback_icon(item.get('issue_type') or ''),
                'context': not bool(item.get('assigned')),
                'done': done,
                'selectable': not done,
                'children': [],
            }

        roots = []
        for key, item in items.items():
            parent = item.get('parent_key')
            trail = {key}
            ancestor = parent
            cyclic = False
            while ancestor in items:
                if ancestor in trail:
                    cyclic = True
                    break
                trail.add(ancestor)
                ancestor = items[ancestor].get('parent_key')
            if parent in nodes and not cyclic:
                nodes[parent]['children'].append(nodes[key])
            else:
                roots.append(nodes[key])

        def sort_children(node):
            node['children'].sort(key=lambda child: child['id'])
            for child in node['children']:
                sort_children(child)

        roots.sort(key=lambda node: node['id'])
        for root in roots:
            sort_children(root)
        return roots
