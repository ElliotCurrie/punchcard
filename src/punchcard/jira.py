"""Jira integration: connection, issue discovery, and runtime ticket cache."""
import json
import logging
import re
from urllib.parse import quote
import requests
from .events import EventHook
from .connectivity import network_error

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
        self._tickets_scope = self._browse_scope()

    def list_tickets(self) -> list[dict]:
        """Return assigned issues only for the currently verified Jira identity."""
        scope = self._browse_scope()
        if not scope or scope != self._tickets_scope:
            return []
        return sorted((ticket.copy() for ticket in self.tickets.values()), key=lambda item: item['issue_key'])

    def get_ticket(self, issue_key: str) -> dict | None:
        """Find a currently verified or scoped locally cached Jira issue."""
        scope = self._browse_scope()
        if not scope:
            return None
        issue_key = issue_key.strip().upper()
        # A freshly browsed/revalidated record overrides an older My Issues snapshot.
        cached = self.database.get_cached_browsed_issue(issue_key, scope)
        if cached:
            return cached
        ticket = self.tickets.get(issue_key) if scope == self._tickets_scope else None
        return ticket.copy() if ticket else None

    def disconnect(self) -> None:
        """Forget Jira credentials and selectable tickets, but retain work history."""
        self.settings.disconnect_jira()
        self.tickets.clear()
        self._tickets_scope = None
        self.last_refreshed = None
        self.on_change.emit()

    def refresh_tickets(self) -> list[dict]:
        """Refresh remote Jira issues, persist them, then replace the runtime cache."""
        try:
            # /myself is authoritative. Reject a different account *before* the
            # assigned-issue refresh can overwrite the local issue-id mapping.
            self.get_current_user()
            scope = self._browse_scope()
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
        if not scope or self._browse_scope() != scope:
            raise ConnectionError('Jira credentials changed while issues were loading. Reconnect to refresh.')
        self.database.save_tickets(issues)
        self.settings.record_connection_check('jira', success=True,
            detail='Jira API verified successfully.')
        self.tickets = {ticket['issue_key']: ticket for ticket in self.database.list_tickets()}
        self._tickets_scope = scope
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
        # A successful search alone cannot prove the token belongs to the
        # original Atlassian account; verify /myself as well.
        try:
            self.get_current_user()
        except Exception as exc:
            self.settings.record_connection_check('jira', success=False, detail=str(exc))
            raise
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


    def get_current_user(self) -> dict:
        """Retrieve the authenticated Jira user independently of issue assignments."""
        jira_url, email, token = self.settings.get_jira_credentials()
        with requests.Session() as session:
            session.auth = (email, token)
            session.headers.update({'Accept': 'application/json'})
            api_base = self._api_base(session, jira_url)
            response = session.get(f'{api_base}/rest/api/3/myself', timeout=(5, 20))
            response.raise_for_status()
            user = response.json()

        account_id = user.get('accountId')
        if not account_id:
            raise ValueError('Jira did not return an Atlassian account ID')

        if self.settings.get_jira_credentials() != (jira_url, email, token):
            raise ConnectionError('Jira credentials changed while verifying the account')
        identity = f'{jira_url.rstrip("/").casefold()}|{email.casefold()}'
        self.database.record_verified_jira_identity(identity, account_id)
        return user


    def get_spaces(self) -> list[dict]:
        """Retrieve accessible Jira spaces, falling back to the local cache when offline."""
        scope = self._browse_scope()
        if not scope:
            raise ValueError('Verify your Jira account before browsing spaces')

        cache_key = 'jira_spaces_cache'
        jira_url, email, token = self.settings.get_jira_credentials()
        spaces = []
        start_at = 0

        try:
            with requests.Session() as session:
                session.auth = (email, token)
                session.headers.update({'Accept': 'application/json'})
                api_base = self._api_base(session, jira_url)

                while True:
                    response = session.get(
                        f'{api_base}/rest/api/3/project/search',
                        params={'startAt': start_at, 'maxResults': 50},
                        timeout=(5, 20),
                    )
                    response.raise_for_status()

                    data = response.json()
                    page = data.get('values', [])

                    spaces.extend({
                        'id': project['id'],
                        'key': project['key'],
                        'name': project['name'],
                    } for project in page)

                    start_at += len(page)
                    if data.get('isLast') or not page or start_at >= data.get('total', float('inf')):
                        break

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            cached = self.database.get_preference(cache_key)

            if cached:
                try:
                    data = json.loads(cached)
                    if data.get('scope') == scope:
                        return data['spaces']
                except (ValueError, KeyError, TypeError):
                    pass

            raise ConnectionError('Jira is unreachable and no cached spaces are available') from exc

        if self._browse_scope() != scope:
            raise ConnectionError('Jira account changed while retrieving spaces')

        spaces.sort(key=lambda space: space['name'].casefold())
        self.database.set_preference(cache_key, json.dumps({
            'scope': scope,
            'spaces': spaces,
        }))

        return spaces


    def get_space_issues(self, space_key: str, page_token: str | None = None,
                         page_size: int = 50) -> dict:
        """Retrieve one page of accessible issues in a Jira space."""
        space_key = space_key.strip().upper()
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', space_key):
            raise ValueError(f'Invalid Jira space key: {space_key}')
        return self._search_page(
            f'project = "{space_key}" ORDER BY updated DESC', page_token, page_size)


    def search_issues(self, query: str, *, assigned_only: bool = False,
                      page_token: str | None = None, page_size: int = 50) -> dict:
        """Search accessible Jira issues across spaces, one page at a time.

        A full issue key uses Jira's issue endpoint for precise lookup, rather
        than relying on text-indexing or the enhanced-search delay.
        """
        query = query.strip()
        if not query:
            return {'issues': [], 'next_page_token': None, 'is_last': True}

        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-\d+', query):
            ticket = self.lookup_issue(query)
            if ticket and (not assigned_only or
                           (ticket['assigned'] and ticket['status_category'] != 'done')):
                return {'issues': [ticket], 'next_page_token': None, 'is_last': True}
            return {'issues': [], 'next_page_token': None, 'is_last': True}

        # Text search treats these terms as data, never as user-supplied JQL.
        # Prefix matching permits searches while the user is still typing.
        terms = re.findall(r'[A-Za-z0-9]+', query)[:6]
        if not terms:
            return {'issues': [], 'next_page_token': None, 'is_last': True}
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-\d*', query):
            condition = f'issueKey ~ "{query.upper()}*"'
        else:
            condition = 'text ~ "' + ' '.join(f'{term}*' for term in terms) + '"'
        if assigned_only:
            condition += ' AND assignee = currentUser() AND statusCategory != Done'

        page = self._search_page(f'{condition} ORDER BY updated DESC', page_token, page_size)
        return {
            'issues': [self._as_cached_ticket(issue) for issue in page['issues']],
            'next_page_token': page['next_page_token'],
            'is_last': page['is_last'],
        }

    def lookup_issue(self, issue_key: str) -> dict | None:
        """Fetch one issue by key, cache it, and return its normal ticket shape."""
        issue_key = issue_key.strip().upper()
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', issue_key):
            raise ValueError('Enter a valid Jira issue key')
        scope = self._browse_scope()
        if not scope:
            raise ValueError('Verify your Jira account before looking up issues')

        jira_url, email, token = self.settings.get_jira_credentials()
        with requests.Session() as session:
            session.auth = (email, token)
            session.headers.update({'Accept': 'application/json'})
            api_base = self._api_base(session, jira_url)
            response = session.get(
                f'{api_base}/rest/api/3/issue/{quote(issue_key, safe="")}',
                params={'fields': FIELDS}, timeout=(5, 20))
            if response.status_code in (403, 404):
                if self._browse_scope() == scope:
                    self.database.revoke_cached_browsed_issue(issue_key, scope)
                    self.tickets.pop(issue_key, None)
                return None
            response.raise_for_status()
            raw = response.json()

        if self._browse_scope() != scope:
            raise ConnectionError('Jira account changed during issue lookup')
        account_id = self.database.get_preference('jira_account_id')
        assigned = bool(account_id) and (
            (raw.get('fields', {}).get('assignee') or {}).get('accountId') == account_id
        )
        issue = self._parse_issue(raw, assigned=assigned)
        self.database.cache_browsed_issues([issue], scope)
        return self._as_cached_ticket(issue)

    def _search_page(self, jql: str, page_token: str | None,
                     page_size: int) -> dict:
        """The shared paginated Jira search transport and scoped cache write."""
        scope = self._browse_scope()
        if not scope:
            raise ValueError('Verify your Jira account before browsing issues')
        jira_url, email, token = self.settings.get_jira_credentials()
        params = {
            'jql': jql,
            'fields': FIELDS,
            'maxResults': max(1, min(page_size, 100)),
        }
        if page_token:
            params['nextPageToken'] = page_token

        with requests.Session() as session:
            session.auth = (email, token)
            session.headers.update({'Accept': 'application/json'})
            api_base = self._api_base(session, jira_url)
            response = session.get(
                f'{api_base}/rest/api/3/search/jql',
                params=params, timeout=(5, 30))
            response.raise_for_status()
            data = response.json()

        account_id = self.database.get_preference('jira_account_id')
        issues = [
            self._parse_issue(
                raw,
                assigned=bool(account_id) and
                (raw.get('fields', {}).get('assignee') or {}).get('accountId') == account_id,
            )
            for raw in data.get('issues', [])
        ]
        if self._browse_scope() != scope:
            raise ConnectionError('Jira account changed while retrieving issues')
        self.database.cache_browsed_issues(issues, scope)
        return {
            'issues': issues,
            'next_page_token': data.get('nextPageToken'),
            'is_last': data.get('isLast', not bool(data.get('nextPageToken'))),
        }

    @staticmethod
    def _as_cached_ticket(issue: dict) -> dict:
        """Convert a Jira API record to the shape used by the picker."""
        return {**{k: v for k, v in issue.items() if k != 'key'},
                'issue_key': issue['key']}

    def search_cached_issues(self, query: str) -> list[dict]:
        """Search the current Jira identity's local issue cache when offline."""
        scope = self._browse_scope()
        return self.database.search_cached_browsed_issues(query, scope) if scope else []

    def _browse_scope(self) -> str | None:
        """Identify the verified Jira site and account owning the browse cache."""
        try:
            jira_url, email, _ = self.settings.get_jira_credentials()
        except ValueError:
            return None

        identity = f'{jira_url.rstrip("/").casefold()}|{email.casefold()}'
        if self.database.get_preference('jira_verified_identity') != identity:
            return None

        account_id = self.database.get_preference('jira_account_id')
        return f'{identity}|{account_id}' if account_id else None


    def reconcile_cached_space(self, space_key: str, issue_keys: set[str]) -> None:
        """Expire old cache entries only once every page of a space was fetched."""
        scope = self._browse_scope()
        if scope:
            self.database.reconcile_cached_space(space_key, scope, issue_keys)

    def get_cached_space_issues(self, space_key: str) -> list[dict]:
        """Return cached issues for a space, including when Jira is offline."""
        scope = self._browse_scope()
        return self.database.list_cached_browsed_issues(
            space_key.strip().upper(), scope
        ) if scope else []
