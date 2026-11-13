"""Short, actionable descriptions of API connectivity failures."""

import requests
from urllib.parse import urlparse


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
