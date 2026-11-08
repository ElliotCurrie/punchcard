"""Windows-only Teams call detection using the local accessibility tree.

Never reads media, chat transcripts, keystrokes, or Teams credentials.  The
heuristic intentionally requires an in-call Leave/Hang up control AND another
call control.  Teams builds may expose different accessible names; see the
optional diagnostic helper in this file for troubleshooting.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Callable

LOG = logging.getLogger(__name__)

# Teams ships both a native host and WebView2 child processes.
TEAMS_EXECUTABLES = {'ms-teams.exe', 'teams.exe', 'msteams.exe'}
END_CALL = re.compile(r'^(?:leave(?: (?:meeting|call|now))?|hang\s*up(?: call)?|end call)(?:\s*\(.*\))?$', re.I)
OTHER_CALL = re.compile(
    r'^(?:mute|unmute|microphone|camera|turn (?:on|off) (?:camera|microphone)|'
    r'share(?: screen)?|stop sharing|open (?:meeting )?chat|'
    r'(?:show|view) participants|participants|people|reactions|more actions|'
    r'more options)(?:\b|$)', re.I,
)
SHARE_WINDOW = re.compile(r'(?:share (?:content|screen|window)|present (?:content|screen)|'
                          r'choose (?:a )?(?:screen|window)|screen sharing)', re.I)
SHARE_CONTROLS = re.compile(r'(?:include computer sound|share (?:screen|window|content)|'
                            r'stop sharing|screen [0-9]+|window [0-9]+)', re.I)



@dataclass(frozen=True)
class CallEvidence:
    window_title: str
    process_name: str
    window_handle: int
    controls: tuple[str, ...]

    def as_metadata(self) -> dict:
        return {
            'source': 'windows_ui_automation',
            'window_title': self.window_title,
            'process_name': self.process_name,
            'window_handle': self.window_handle,
            'call_controls': list(self.controls),
        }

    @property
    def title(self) -> str:
        raw = self.window_title.strip()
        if not raw or raw.lower() in ('microsoft teams', 'teams'):
            return 'Teams call'
        # E.g. 'Project stand-up | Microsoft Teams'
        candidate = re.sub(r'\s*[-|–]\s*Microsoft Teams\s*$', '', raw, flags=re.I).strip()
        return candidate or 'Teams call'


def is_call_controls(names: list[str] | tuple[str, ...]) -> bool:
    """Distinguish an active call from Teams chat and a pre-join screen."""
    labels = [n.strip() for n in names if n and n.strip()]
    return any(END_CALL.fullmatch(label) for label in labels) and any(
        OTHER_CALL.match(label) for label in labels
    )


def _is_teams_process(pid: int) -> bool:
    import psutil
    try:
        process = psutil.Process(pid)
        for _ in range(5):
            if process.name().lower() in TEAMS_EXECUTABLES:
                return True
            process = process.parent()
            if process is None:
                return False
    except (psutil.Error, OSError):
        pass
    return False


def inspect_teams_windows() -> list[tuple[int, str, str, tuple[str, ...]]]:
    """Read Teams window titles and accessible button labels only.

    This can help identify call-control labels that changed after a Teams update.
    Invoked on a COM-initialised Windows thread.
    """
    if os.name != 'nt':
        return []

    import win32gui
    import win32process
    from pywinauto import Desktop
    import psutil

    candidate_windows: list[tuple[int, str, int]] = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if _is_teams_process(pid):
            candidate_windows.append((hwnd, title, pid))

    win32gui.EnumWindows(collect, None)
    uia = Desktop(backend='uia')
    found = []
    for hwnd, title, pid in candidate_windows:
        try:
            buttons = uia.window(handle=hwnd).descendants(control_type='Button')
            names = tuple(dict.fromkeys(
                name for button in buttons[:150]
                if (name := (button.window_text() or '').strip())
            ))
            found.append((hwnd, title, psutil.Process(pid).name(), names))
        except Exception:
            LOG.debug('Unable to inspect Teams window %s', hwnd, exc_info=True)
    return found


@dataclass(frozen=True)
class TeamsObservation:
    """Call evidence plus signs that a Teams sharing overlay obscures controls."""
    call: CallEvidence | None
    sharing_open: bool = False


def probe_teams_call() -> TeamsObservation:
    """Recognise calls from controls; recognise sharing UI only as continuity evidence."""
    found = inspect_teams_windows()
    sharing = False
    call = None
    for hwnd, title, process, controls in found:
        if is_call_controls(controls):
            call = CallEvidence(title, process, hwnd, controls[:40])
        # A sharing picker can hide the Leave button. It is never enough to
        # start a call; it can only keep an already-confirmed call open.
        if SHARE_WINDOW.search(title) or (
            any(SHARE_CONTROLS.search(label) for label in controls)
            and any(('screen' in label.lower() or 'window' in label.lower())
                    for label in controls)
        ):
            sharing = True
    return TeamsObservation(call=call, sharing_open=sharing)


def diagnose():
    """One-shot Teams accessibility check, for use during an active call."""
    if os.name != 'nt':
        print('Teams diagnostics require Windows.')
        return
    import pythoncom
    pythoncom.CoInitialize()
    try:
        found = inspect_teams_windows()
        if not found:
            print('No accessible desktop Teams windows found.')
        for hwnd, title, process, labels in found:
            print(f'\nWindow: {title!r} (process={process}, hwnd={hwnd})')
            print('Recognised active call:', is_call_controls(labels))
            print('Buttons:', ', '.join(labels[:80]) or '(none exposed)')
    finally:
        pythoncom.CoUninitialize()


class TeamsCallMonitor:
    """Polls Teams window accessibility; emits debounced start/update/end."""

    def __init__(self, on_event: Callable[[str, CallEvidence | None], None],
                 *, probe: Callable[[], TeamsObservation | CallEvidence | None] = probe_teams_call,
                 interval: float = 3, start_confirm: int = 2, end_confirm: int = 10):
        self.on_event = on_event
        self.probe = probe
        self.interval = interval
        self.start_confirm = start_confirm
        self.end_confirm = end_confirm
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active: CallEvidence | None = None
        self._present_count = 0
        self._absent_count = 0

    def start(self):
        if os.name != 'nt':
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='PunchCardTeamsMonitor')
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def observe(self, observation: TeamsObservation | CallEvidence | None):
        """Debounce UI changes; sharing overlays preserve one continuous call."""
        if isinstance(observation, TeamsObservation):
            evidence = observation.call
            sharing = observation.sharing_open
        else:
            evidence, sharing = observation, False
        if evidence:
            self._present_count += 1
            self._absent_count = 0
            if self._active is None:
                if self._present_count >= self.start_confirm:
                    self._active = evidence
                    self.on_event('start', evidence)
            elif evidence != self._active:
                self._active = evidence
                self.on_event('update', evidence)
        else:
            self._present_count = 0
            if sharing and self._active is not None:
                self._absent_count = 0
                return
            self._absent_count += 1
            if self._active is not None and self._absent_count >= self.end_confirm:
                self._active = None
                self._absent_count = 0
                self.on_event('end', None)

    def _run(self):
        # pywinauto's UIA backend requires COM initialisation on this thread.
        import pythoncom
        pythoncom.CoInitialize()
        try:
            while not self._stop.is_set():
                try:
                    self.observe(self.probe())
                except Exception:
                    LOG.exception('Teams monitoring failed; retrying')
                    # Do not treat probing errors as evidence that a call ended.
                self._stop.wait(self.interval)
        finally:
            pythoncom.CoUninitialize()


if __name__ == '__main__':
    diagnose()
