"""One Jira issue tree, grouped by space with lazy browsing and global search."""
import asyncio
from collections import defaultdict

import requests
from nicegui import ui

from .layout import window_layout


class TicketPicker:
    def __init__(self, desktop, jira, connectivity, *, on_start_tracking):
        self.desktop = desktop
        self.jira = jira
        self.connectivity = connectivity
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
        return bool(self.selected_ticket_key and
                    self.on_start_tracking(self.selected_ticket_key))

    @staticmethod
    def _space_key(issue_key: str) -> str:
        return issue_key.rsplit('-', 1)[0] if '-' in issue_key else ''

    def register_page(self) -> None:
        @ui.page('/tickets')
        def render_ticket_picker():
            ui.add_css('.q-tree__node-header{min-height:36px;}')
            state = {
                'assigned_only': True,
                'query': '',
                'spaces': [],
                'spaces_loaded': False,
                'spaces_loading': False,
                'issues_by_space': {},
                'loaded_spaces': set(),
                'loading_spaces': set(),
                'next_tokens': {},
                'expanded': set(),
                'initial_expansion': False,
                'search_results': [],
                'search_next_token': None,
                'search_generation': 0,
                'search_task': None,
            }

            def jira_offline() -> bool:
                return self.connectivity.status('jira') == 'offline'

            def space_metadata() -> dict[str, str]:
                result = {space['key']: space['name'] for space in state['spaces']}
                for ticket in self.jira.list_tickets():
                    key = self._space_key(ticket['issue_key'])
                    if key:
                        result.setdefault(key, key)
                for ticket in state['search_results']:
                    key = self._space_key(ticket['issue_key'])
                    if key:
                        result.setdefault(key, key)
                return result

            def issue_candidates() -> dict[str, dict]:
                if state['assigned_only']:
                    items = self.jira.list_tickets()
                    # Search can discover newly assigned issues before the next My Issues refresh.
                    items += [ticket for ticket in state['search_results']
                              if ticket.get('assigned') and ticket.get('status_category') != 'done']
                else:
                    if state['query']:
                        # Cached search results are shown only when offline. Online
                        # searches must not leak stale, inaccessible cached issues.
                        items = self.jira.search_cached_issues(state['query']) if jira_offline() else []
                        query = state['query'].casefold()
                        spaces = space_metadata()
                        items += [ticket for key, issues in state['issues_by_space'].items()
                                  if query in key.casefold() or query in spaces.get(key, key).casefold()
                                  for ticket in issues]
                    else:
                        items = [ticket for issues in state['issues_by_space'].values()
                                 for ticket in issues]
                    items += state['search_results']
                return {ticket['issue_key']: ticket for ticket in items}

            def build_nodes() -> list[dict]:
                spaces = space_metadata()
                query = state['query'].casefold()
                grouped = defaultdict(list)
                for ticket in issue_candidates().values():
                    key = self._space_key(ticket['issue_key'])
                    if key:
                        grouped[key].append(ticket)
                        spaces.setdefault(key, key)

                if state['assigned_only']:
                    keys = set(grouped)
                else:
                    keys = set(spaces)

                nodes = []
                for key in sorted(keys, key=lambda k: (spaces[k].casefold(), k)):
                    space_matches = query and (query in key.casefold() or
                                               query in spaces[key].casefold())
                    tickets = grouped[key]
                    if query and not space_matches:
                        tickets = [ticket for ticket in tickets
                                   if query in ticket['issue_key'].casefold()
                                   or query in ticket['title'].casefold()]
                        if not tickets:
                            continue
                    elif query and space_matches and not tickets and state['assigned_only']:
                        continue

                    children = self._build_issue_tree(
                        tickets, show_parent_context=state['assigned_only'])
                    if not state['assigned_only'] and not query:
                        if key in state['loading_spaces']:
                            children.append(self._message_node(key, 'Loading issues...'))
                        elif key not in state['loaded_spaces'] and not children:
                            children.append(self._message_node(key, 'Expand to load issues'))
                        elif key in state['loaded_spaces'] and not children:
                            children.append(self._message_node(key, 'No cached issues'))
                        if state['next_tokens'].get(key):
                            children.append({
                                'id': f'more:{key}', 'kind': 'more', 'key': key,
                                'label': 'Load more issues', 'title': 'Load more issues',
                                'selectable': True, 'children': [],
                            })
                    # For matching spaces, keep them expandable even before issues load.
                    if query and not children and space_matches:
                        children = [self._message_node(key, 'Expand to load issues')]
                    nodes.append({
                        'id': f'space:{key}', 'kind': 'space', 'key': key,
                        'label': f'{spaces[key]} ({key})' if spaces[key] != key else key,
                        'title': spaces[key], 'selectable': False,
                        'children': children,
                    })
                return nodes

            def refresh_tree() -> None:
                nodes = build_nodes()
                if state['assigned_only'] and not state['initial_expansion']:
                    state['expanded'] = {node['id'] for node in nodes}
                    state['initial_expansion'] = True
                tree.props['nodes'] = nodes
                tree.update()
                if state['query']:
                    # Show matching issues immediately, but leave space-name-only
                    # matches collapsed so a user can expand to load their issues.
                    tree.expand([node['id'] for node in nodes
                                 if any(child.get('kind') == 'issue' for child in node['children'])])
                elif state['expanded']:
                    tree.expand(sorted(state['expanded']))
                search_more.set_visibility(bool(state['query'] and state['search_next_token'])
                                           and not jira_offline())
                empty_label.set_visibility(not bool(nodes))
                if not nodes:
                    empty_label.set_text(
                        'No matching spaces or issues.' if state['query'] else
                        'No assigned issues. Untick Assigned to me to browse spaces.'
                        if state['assigned_only'] else
                        'No Jira spaces available. Verify your Jira connection.'
                    )

            def refresh_offline_notice() -> None:
                offline_notice.set_visibility(jira_offline())

            async def load_spaces() -> None:
                if state['spaces_loading']:
                    return
                state['spaces_loading'] = True
                try:
                    spaces = await asyncio.to_thread(self.jira.get_spaces)
                    state['spaces'] = spaces
                    state['spaces_loaded'] = True
                    refresh_tree()
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                        ConnectionError):
                    # Previously fetched assigned issues remain usable without a connection.
                    refresh_tree()
                except Exception as exc:
                    ui.notify(f'Could not load Jira spaces: {exc}', type='warning')
                finally:
                    state['spaces_loading'] = False

            async def load_space(space_key: str, *, reset: bool = False) -> None:
                if space_key in state['loading_spaces']:
                    return
                if reset or space_key not in state['issues_by_space']:
                    state['issues_by_space'][space_key] = self.jira.get_cached_space_issues(space_key)
                if jira_offline():
                    refresh_tree()
                    return
                if not reset and space_key in state['loaded_spaces'] and not state['next_tokens'].get(space_key):
                    return

                state['loading_spaces'].add(space_key)
                refresh_tree()
                try:
                    token = None if reset else state['next_tokens'].get(space_key)
                    page = await asyncio.to_thread(
                        self.jira.get_space_issues, space_key, token, 50)
                    # The online tree shows only pages confirmed by Jira during
                    # this browse, not every historical cached issue in SQLite.
                    fetched = [{**issue, 'issue_key': issue['key']} for issue in page['issues']]
                    if token:
                        existing = {issue['issue_key']: issue for issue in state['issues_by_space'][space_key]}
                        existing.update({issue['issue_key']: issue for issue in fetched})
                        state['issues_by_space'][space_key] = list(existing.values())
                    else:
                        state['issues_by_space'][space_key] = fetched
                    state['next_tokens'][space_key] = page['next_page_token'] if not page['is_last'] else None
                    state['loaded_spaces'].add(space_key)
                    if page['is_last']:
                        # Only a fully traversed space proves which cached issues
                        # are no longer returned by Jira. Partial pages do not.
                        await asyncio.to_thread(
                            self.jira.reconcile_cached_space, space_key,
                            {issue['issue_key'] for issue in state['issues_by_space'][space_key]},
                        )
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                        ConnectionError):
                    # Keep cached issues displayed and allow retry after reconnecting.
                    pass
                except Exception as exc:
                    ui.notify(f'Could not load {space_key}: {exc}', type='negative')
                finally:
                    state['loading_spaces'].discard(space_key)
                    refresh_tree()

            async def expand_nodes(e) -> None:
                expanded = {key for key in (e.value or []) if key.startswith('space:')}
                previous = state['expanded']
                state['expanded'] = expanded
                if state['assigned_only']:
                    return
                for node in sorted(expanded - previous):
                    space_key = node.removeprefix('space:')
                    if space_key in state['loaded_spaces']:
                        continue
                    if state['query']:
                        # Search automatically expands matching issues. Fetch all
                        # issues only when opening a space matched by its name/key.
                        space = space_metadata().get(space_key, space_key)
                        text = state['query'].casefold()
                        if text not in space.casefold() and text not in space_key.casefold():
                            continue
                        if any(self._space_key(item['issue_key']) == space_key
                               for item in state['search_results']):
                            continue
                    await load_space(space_key)

            async def select_node(e) -> None:
                key = e.value
                if not key:
                    return
                if key.startswith('more:'):
                    await load_space(key.removeprefix('more:'))
                    tree.deselect()
                    return
                if key.startswith('space:') or key.startswith('message:'):
                    tree.deselect()
                    return
                # A cached issue may no longer be accessible. Confirm permissions
                # and status before an online selection; offline selections keep
                # working against the scoped local cache.
                if not jira_offline():
                    try:
                        live = await asyncio.to_thread(self.jira.lookup_issue, key)
                    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                            ConnectionError):
                        live = self.jira.get_ticket(key)  # connectivity may have just dropped
                    except Exception as exc:
                        ui.notify(f'Could not verify Jira issue: {exc}', type='warning')
                        tree.deselect()
                        return
                    if live is None:
                        ui.notify('Jira issue is no longer accessible', type='warning')
                        # Also remove the revoked issue from this browser window;
                        # database cache eviction alone does not update UI state.
                        for space_key, issues in state['issues_by_space'].items():
                            state['issues_by_space'][space_key] = [
                                issue for issue in issues if issue['issue_key'] != key
                            ]
                        state['search_results'] = [
                            issue for issue in state['search_results'] if issue['issue_key'] != key
                        ]
                        tree.deselect()
                        refresh_tree()
                        return
                ticket = self.jira.get_ticket(key)
                if not ticket or ticket['status_category'] == 'done':
                    ui.notify('This Jira issue is no longer selectable', type='warning')
                    tree.deselect()
                    return
                if not self.select_ticket(key) or not self.start_tracking_selected():
                    ui.notify('Could not start tracking this issue', type='warning')
                    tree.deselect()
                    return
                ui.notify(f'Selected {key}', type='positive')
                self.desktop.command('hide', window='tickets')

            async def perform_search(generation: int, *, next_page: bool = False) -> None:
                try:
                    if not next_page:
                        await asyncio.sleep(0.45)
                    query = state['query']
                    if len(query) < 2 or jira_offline():
                        return
                    token = state['search_next_token'] if next_page else None
                    page = await asyncio.to_thread(
                        self.jira.search_issues, query,
                        assigned_only=state['assigned_only'], page_token=token, page_size=50)
                    if generation != state['search_generation']:
                        return
                    if next_page:
                        old = {item['issue_key']: item for item in state['search_results']}
                        old.update({item['issue_key']: item for item in page['issues']})
                        state['search_results'] = list(old.values())
                    else:
                        state['search_results'] = page['issues']
                    state['search_next_token'] = (
                        None if page['is_last'] else page['next_page_token'])
                    refresh_tree()
                except asyncio.CancelledError:
                    raise
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                        ConnectionError):
                    # Local cached searching continues to work while offline.
                    pass
                except Exception as exc:
                    if generation == state['search_generation']:
                        ui.notify(f'Jira search failed: {exc}', type='warning')

            def schedule_search() -> None:
                state['search_generation'] += 1
                task = state['search_task']
                if task and not task.done():
                    task.cancel()
                state['search_results'] = []
                state['search_next_token'] = None
                refresh_tree()
                if state['query'] and not jira_offline():
                    state['search_task'] = asyncio.create_task(
                        perform_search(state['search_generation']))

            def change_query(e) -> None:
                state['query'] = (e.value or '').strip()
                if not state['query']:
                    state['expanded'].clear()
                    state['initial_expansion'] = False
                    tree.collapse()
                schedule_search()

            def change_assignment(e) -> None:
                state['assigned_only'] = bool(e.value)
                state['expanded'] = set()
                state['initial_expansion'] = False
                tree.collapse()
                schedule_search()
                if not state['spaces_loaded']:
                    asyncio.create_task(load_spaces())

            async def refresh_data() -> None:
                refresh_button.disable()
                try:
                    if jira_offline():
                        ui.notify('Offline - showing cached issues', type='warning')
                        return
                    if state['assigned_only']:
                        await asyncio.to_thread(self.jira.refresh_tickets)
                    else:
                        await load_spaces()
                        for node in sorted(state['expanded']):
                            await load_space(node.removeprefix('space:'), reset=True)
                    if state['query']:
                        schedule_search()
                    refresh_tree()
                except Exception as exc:
                    ui.notify(f'Could not refresh Jira: {exc}', type='warning')
                finally:
                    refresh_button.enable()

            with window_layout(self.desktop, 'tickets', 'PUNCHCARD / ISSUES', padding=18):
                with ui.row().classes('items-center justify-between w-full'):
                    ui.label('Select Jira issue').classes('text-h6')
                    refresh_button = ui.button(icon='refresh', on_click=refresh_data).props('flat dense round')

                ui.input('Search Jira', placeholder='Issue key, summary or space',
                         on_change=change_query).props('outlined dense clearable').classes('w-full')
                offline_notice = ui.label('Offline - showing cached issues').classes(
                    'text-caption text-amber-8')
                refresh_offline_notice()
                ui.checkbox('Assigned to me', value=True,
                            on_change=change_assignment).classes('text-body2')

                tree = ui.tree([], on_select=select_node, on_expand=expand_nodes).classes('w-full')
                tree.props('dense')
                tree.add_slot('default-header', """
                    <div class="row items-center no-wrap" style="gap:8px;min-width:0;max-width:100%;"
                         :style="props.node.done ? 'opacity:0.5' : ''">
                        <q-icon v-if="props.node.kind === 'space'" name="folder"
                                size="18px" color="blue-grey-7" />
                        <q-icon v-else-if="props.node.kind === 'more'" name="expand_more"
                                size="17px" color="blue-grey-7" />
                        <img v-else-if="props.node.icon_url" :src="props.node.icon_url"
                             @error="props.node.icon_url = ''"
                             style="height:17px;width:17px;object-fit:contain;flex-shrink:0" />
                        <q-icon v-else-if="props.node.kind === 'issue'"
                                :name="props.node.fallback_icon" size="17px" color="blue-grey-7" />
                        <div class="ellipsis" :title="props.node.label">
                            <template v-if="props.node.kind === 'issue'">
                                <strong>{{ props.node.key }}</strong> · {{ props.node.title }}
                            </template>
                            <span v-else>{{ props.node.label }}</span>
                        </div>
                        <q-badge v-if="props.node.done" color="grey-6" label="Done" />
                        <q-badge v-else-if="props.node.context" color="blue-grey-3"
                                 text-color="blue-grey-9" label="Parent" />
                    </div>
                """)
                empty_label = ui.label('').classes('text-grey-7')
                search_more = ui.button('Load more search results', icon='expand_more',
                    on_click=lambda: asyncio.create_task(
                        perform_search(state['search_generation'], next_page=True)))
                search_more.props('flat dense').classes('w-full')
                search_more.set_visibility(False)
                ui.label('Select any unfinished issue to start tracking.').classes(
                    'text-caption text-grey-7')

                refresh_tree()
                ui.timer(0.1, load_spaces, once=True)
                ui.timer(1.0, refresh_offline_notice)
                revision = {'value': self._revision}

                def refresh_changed():
                    if revision['value'] != self._revision:
                        revision['value'] = self._revision
                        refresh_tree()

                ui.timer(1.0, refresh_changed)

    @staticmethod
    def _message_node(space_key: str, message: str) -> dict:
        return {
            'id': f'message:{space_key}', 'kind': 'message', 'key': '',
            'label': message, 'title': message, 'selectable': False,
            'children': [],
        }

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
    def _build_issue_tree(cls, tickets: list[dict], *, show_parent_context: bool = True) -> list[dict]:
        """Create the existing issue/parent hierarchy below a space node."""
        items = {t['issue_key']: t for t in tickets}
        nodes = {}
        for key, item in items.items():
            done = item.get('status_category') == 'done'
            nodes[key] = {
                'id': key, 'kind': 'issue', 'key': key,
                'label': f"{key} · {item['title']}",
                'title': item['title'],
                'type': item.get('issue_type') or 'Issue',
                'status': item.get('status') or '',
                'icon_url': item.get('issue_type_icon') or '',
                'fallback_icon': cls._fallback_icon(item.get('issue_type') or ''),
                'context': show_parent_context and not bool(item.get('assigned')),
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
