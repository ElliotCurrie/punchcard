"""Shared network reachability state for PunchCard."""
import requests
from urllib.parse import urlparse


class Connectivity:
    def __init__(self, settings):
        self.settings = settings
        self._states = {'jira': 'checking', 'tempo': 'checking'}

    def check(self) -> None:
        config = self.settings.load()
        endpoints = {
            'jira': config.get('jira_url', ''),
            'tempo': config.get('tempo_api_url', 'https://api.tempo.io'),
        }

        states = {}
        for service, url in endpoints.items():
            if not url or not self.settings.connection_status(service)['configured']:
                states[service] = 'not_configured'
                continue

            try:
                requests.head(url, timeout=(3, 5), allow_redirects=False)
            except requests.exceptions.SSLError:
                states[service] = 'error'
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                states[service] = 'offline'
            except requests.exceptions.RequestException:
                states[service] = 'error'
            else:
                states[service] = 'online'

        self._states = states

    def status(self, service: str | None = None) -> str:
        if service:
            return self._states[service]

        active = [
            state for state in self._states.values()
            if state != 'not_configured'
        ]

        if not active:
            return 'not_configured'
        if all(state == 'online' for state in active):
            return 'online'
        if all(state == 'offline' for state in active):
            return 'offline'
        if 'checking' in active:
            return 'checking'
        return 'limited'


def network_error(service: str, url: str, exc: requests.RequestException) -> str:
    """Keep verbose transport errors out of the UI while retaining the cause."""
    host = urlparse(url).hostname or service
    message = str(exc).lower()
    if isinstance(exc, requests.exceptions.Timeout):
        reason = f'Timed out connecting to {host}.'
    elif any(term in message for term in ('nameresolutionerror', 'getaddrinfo failed', 'failed to resolve',
                                         'name or service not known', 'nodename nor servname provided')):
        reason = f'Could not resolve {host}.'
    elif isinstance(exc, requests.exceptions.SSLError):
        reason = f'Secure connection to {host} failed.'
    else:
        reason = f'Could not reach {host}.'
    return f'{service} network error: {reason} Check your network connection.'
