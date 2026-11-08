"""Small synchronous notifications for independently owned application state.

Domain objects emit events without importing NiceGUI. Application subscribes
collaborators; a view decides how/when to refresh its own controls.
"""
from __future__ import annotations

import logging
from threading import RLock
from typing import Callable

LOG = logging.getLogger(__name__)


class EventHook:
    """Subscribe to a state change; callbacks cannot roll back committed work.

    `subscribe()` returns an unsubscribe callback so views with shorter lifetimes
    can detach. Dispatch uses a snapshot, permitting changes during notification.
    """

    def __init__(self, name: str):
        self.name = name
        self._listeners: list[Callable] = []
        self._lock = RLock()

    def subscribe(self, callback: Callable) -> Callable[[], None]:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)
        return unsubscribe

    def emit(self, *args, **kwargs) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for callback in listeners:
            try:
                callback(*args, **kwargs)
            except Exception:
                LOG.exception('Event subscriber failed: %s', self.name)
