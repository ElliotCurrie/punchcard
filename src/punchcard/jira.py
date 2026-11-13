"""Jira integration: connection, issue discovery, and runtime ticket cache."""
import logging
from urllib.parse import quote
import requests
from .events import EventHook
from .connection_errors import network_error

LOG = logging.getLogger(__name__)
JQL = 'assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC'
FIELDS = 'summary,status,issuetype,parent,assignee'


class Jira:
    """Own Jira connectivity and the issues retained during application runtime."""

    def __init__(self, settings, database):
        self.settings = settings
        self.on_change = EventHook("jira.tickets_changed")
        self.database = database
        self.tickets: dict[str, dict] = {
            issue['issue_key']: issue for issue in database.list_tickets()
        }
        self.last_refreshed = database.get_preference('last_ticket_refresh') or None

    def list_tickets(self) -> list[dict]:
        """Return cached active Jira tickets (including ancestor issues)."""
        return sorted((ticket.copy() for ticket in self.tickets.values()), key=lambda item: item['issue_key'])

    def get_ticket(self, issue_key: str) -> dict | None:
        ticket = self.tickets.get(issue_key)
        return ticket.copy() if ticket else None

    def search_tickets(self, query: str) -> list[dict]:
        needle = query.casefold().strip()
        return [ticket for ticket in self.list_tickets()
                if needle in ticket['issue_key'].casefold() or needle in ticket['title'].casefold()]

    def disconnect(self) -> None:
        """Forget Jira credentials and selectable tickets, but retain work history."""
        self.settings.disconnect_jira()
        self.database.save_tickets([])
        self.database.set_preference('last_ticket', '')
        self.database.set_preference('last_ticket_refresh', '')
        self.tickets.clear()
        self.last_refreshed = None
        self.on_change.emit()

    def refresh_tickets(self) -> list[dict]:
        """Refresh remote Jira issues, persist them, then replace the runtime cache."""
        try:
            issues = self._fetch_tickets()
        except Exception as exc:
            url = self.settings.load().get('jira_url', '')
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                code = exc.response.status_code
                detail = (f'Jira authentication or permission error (HTTP {code})'
                          if code in (401, 403) else f'Jira API error (HTTP {code})')
            elif isinstance(exc, requests.RequestException):
                detail = network_error('Jira', url, exc)
            else:
                detail = f'Jira issue request failed: {exc}'
            self.settings.record_connection_check('jira', success=False, detail=detail)
            raise ConnectionError(detail) from exc
        # A request may finish after Jira was disconnected in another window.
        # Never repopulate its selectable cache with stale credentials.
        if not self.settings.connection_status('jira')['configured']:
            raise ConnectionError('Jira was disconnected while issues were loading. Reconnect to refresh.')
        self.database.save_tickets(issues)
        self.settings.record_connection_check('jira', success=True,
            detail='Jira API verified successfully.')
        self.tickets = {ticket['issue_key']: ticket for ticket in self.database.list_tickets()}
        self.last_refreshed = self.database.get_preference('last_ticket_refresh') or None
        self.on_change.emit()
        return issues

    def check_connection(self) -> None:
        """Read-only Jira check, without changing the ticket cache."""
        try:
            jira_url, email, token = self.settings.get_jira_credentials()
            with requests.Session() as session:
                session.auth = (email, token)
                session.headers.update({'Accept': 'application/json'})
                base = self._api_base(session, jira_url)
                response = session.get(f'{base}/rest/api/3/search/jql',
                    params={'jql': JQL, 'fields': 'summary', 'maxResults': 1},
                    timeout=(5, 20))
                response.raise_for_status()
        except Exception as exc:
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                code = exc.response.status_code
                detail = (f'Jira authentication or permission error (HTTP {code})'
                          if code in (401, 403) else f'Jira API error (HTTP {code})')
            elif isinstance(exc, requests.RequestException):
                detail = network_error('Jira', self.settings.load().get('jira_url', ''), exc)
            else:
                detail = str(exc)
            self.settings.record_connection_check('jira', success=False, detail=detail)
            raise ConnectionError(detail) from exc
        self.settings.record_connection_check('jira', success=True,
            detail='Jira API verified successfully.')

    def _api_base(self, session: requests.Session, jira_site_url: str) -> str:
        """Scoped Jira API tokens use the Atlassian gateway."""
        response = session.get(f'{jira_site_url}/_edge/tenant_info', timeout=15)
        response.raise_for_status()
        cloud_id = response.json().get('cloudId')
        if not cloud_id:
            raise RuntimeError('Could not determine Jira cloudId from /_edge/tenant_info')
        return f'https://api.atlassian.com/ex/jira/{cloud_id}'

    def _parse_issue(self, issue: dict, *, assigned: bool) -> dict:
        fields = issue.get('fields') or {}
        issue_type = fields.get('issuetype') or {}
        status = fields.get('status') or {}
        category = status.get('statusCategory') or {}
        parent = fields.get('parent') or {}
        return {
            'key': issue['key'],
            'jira_issue_id': str(issue.get('id') or ''),
            'account_id': (fields.get('assignee') or {}).get('accountId') if assigned else None,
            'title': fields.get('summary') or issue['key'],
            'status': status.get('name') or '',
            'status_category': (category.get('key') or '').lower(),
            'parent_key': parent.get('key') or None,
            'issue_type': issue_type.get('name') or 'Issue',
            'issue_type_icon': issue_type.get('iconUrl') or '',
            'assigned': assigned,
        }

    def _is_done(self, issue: dict) -> bool:
        return issue['status_category'] == 'done'

    def _fetch_tickets(self) -> list[dict]:
        """Fetch unfinished issues assigned to current user; add missing parents.

        Parent issues may be unassigned or completed. Completed parents are kept for
        tree context but cannot be selected for new time tracking. No unrelated
        sibling issues are fetched.
        """
        jira_site_url, email, token = self.settings.get_jira_credentials()
        session = requests.Session()
        session.auth = (email, token)
        session.headers.update({'Accept': 'application/json'})

        issues: dict[str, dict] = {}
        next_page = None
        seen_tokens = set()
        try:
            api_base = self._api_base(session, jira_site_url)
            while True:
                params = {'jql': JQL, 'fields': FIELDS, 'maxResults': 100}
                if next_page:
                    params['nextPageToken'] = next_page
                response = session.get(f'{api_base}/rest/api/3/search/jql', params=params, timeout=30)
                response.raise_for_status()
                payload = response.json()
                for raw in payload.get('issues', []):
                    issue = self._parse_issue(raw, assigned=True)
                    # JQL filters Done. Also guard against inconsistent API results.
                    if not self._is_done(issue):
                        issues[issue['key']] = issue
                next_page = payload.get('nextPageToken')
                if payload.get('isLast') or not next_page:
                    break
                if next_page in seen_tokens:
                    raise RuntimeError('Jira returned the same pagination token repeatedly')
                seen_tokens.add(next_page)

            # Resolve parent -> grandparent -> ... even when the parent isn't assigned.
            # This is bounded to avoid endless loops on corrupt relationships.
            attempted = set()
            for _ in range(12):
                parents = sorted({issue['parent_key'] for issue in issues.values()
                                  if issue.get('parent_key') and issue['parent_key'] not in issues
                                  and issue['parent_key'] not in attempted})
                if not parents:
                    break
                for key in parents:
                    attempted.add(key)
                    try:
                        response = session.get(
                            f'{api_base}/rest/api/3/issue/{quote(key, safe="")}',
                            params={'fields': FIELDS}, timeout=20)
                        if response.status_code in (403, 404):
                            LOG.info('No permission to load parent issue %s', key)
                            continue
                        response.raise_for_status()
                        parent_issue = self._parse_issue(response.json(), assigned=False)
                        issues[parent_issue['key']] = parent_issue
                    except requests.RequestException:
                        LOG.warning('Could not load Jira parent %s; showing child as root', key, exc_info=True)

        finally:
            session.close()
        return sorted(issues.values(), key=lambda t: t['key'])

